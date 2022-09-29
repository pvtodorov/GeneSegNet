import time, os
from scipy.ndimage.filters import maximum_filter1d
import torch
import scipy.ndimage
import numpy as np
import tifffile
from tqdm import trange
import matplotlib.pyplot as plt
from numba import njit, float32, int32, vectorize
import cv2
import fastremap
from scipy.ndimage.filters import gaussian_filter
import scipy.cluster.hierarchy as hcluster
from numpy.core.records import fromarrays
import math
import kornia
from morphology import Dilation2d

import logging
dynamics_logger = logging.getLogger(__name__)

import utils, metrics, transforms, plot
from torch import optim, nn
import resnet_torch
TORCH_ENABLED = True 
torch_GPU = torch.device('cuda')
torch_CPU = torch.device('cpu')

def mat_math (intput, str):
    if str=="atan":
        output = torch.atan(intput) 
    if str=="sqrt":
        output = torch.sqrt(intput) 
    return output

def level_set(LSF, img, mu, nu, epison, step):
    Drc = (epison / math.pi) / (epison*epison+ LSF*LSF)
    Hea = 0.5*(1 + (2 / math.pi) * mat_math(LSF/epison, "atan")) 
    Iys = torch.gradient(LSF, dim=2)[0]
    Ixs = torch.gradient(LSF, dim=3)[0]
    s = mat_math(Ixs*Ixs+Iys*Iys, "sqrt") 
    Nx = Ixs / (s+0.000001) 
    Ny = Iys / (s+0.000001)

    Mxx = torch.gradient(Nx, dim=2)[0]
    Nxx = torch.gradient(Nx, dim=3)[0]

    Nyy = torch.gradient(Ny, dim=2)[0]
    Myy = torch.gradient(Ny, dim=3)[0]

    cur = Nxx + Nyy
    Length = nu*Drc*cur 
    
    Lap = kornia.filters.laplacian(LSF, 3)
    Penalty = mu*(Lap - cur) 

    s1=Hea*img 
    s2=(1-Hea)*img 
    s3=1-Hea 
    C1 = s1.sum()/ Hea.sum() 
    C2 = s2.sum()/ s3.sum() 
    CVterm = Drc*(-1 * (img - C1)*(img - C1) + 1 * (img - C2)*(img - C2)) 

    LSF = LSF + step*(Length + Penalty + CVterm) 
    return LSF 


def postprocess(mask, N, device='cpu'):
    if N == 1:
        print("@@@@@@@@@@@@@@@@@Initial dilation@@@@@@@@@@@@@@@@@@")
        dilation = Dilation2d(1,1,5,soft_max=False).to(device)

    # plt.imshow(mask)
    # plt.axis('off')
    # plt.savefig("/home/yw2009/anaconda3/envs/medicalimage/cellposes/Gseg_2C_levelset_offset/levelset/mask.png", bbox_inches='tight', pad_inches = 0 )
    # plt.clf()

    mask = torch.from_numpy(mask.astype(np.int32)).unsqueeze(0).unsqueeze(0).to(device)
    cell_ids = torch.unique(mask)[1:]
    # print("cell_ids:", cell_ids)
    
    mu = 1 
    nu = 0.003 * 255 * 255 
    num = 10
    epison = 1 
    step = 0.01

    new_mask = torch.zeros((mask.shape[2],mask.shape[3]), dtype=torch.int32).to(device)
    for cell_id in cell_ids:
        img = ((mask == cell_id)*255).float()
        LSF = ((mask == cell_id)*1).float()

        # plt.imshow(img.detach().squeeze().cpu().numpy())
        # plt.axis('off')
        # plt.savefig("/home/yw2009/anaconda3/envs/medicalimage/cellposes/Gseg_2C_levelset_offset/levelset/img_{}.png".format(cell_id), bbox_inches='tight', pad_inches = 0 )
        # plt.clf()

        # plt.imshow(LSF.detach().squeeze().cpu().numpy())
        # plt.axis('off')
        # plt.savefig("/home/yw2009/anaconda3/envs/medicalimage/cellposes/Gseg_2C_levelset_offset/levelset/LSF_{}.png".format(cell_id), bbox_inches='tight', pad_inches = 0 )
        # plt.clf()

        for i in range(1,num):
            if N==1 and i == 1:
                print("@@@@@@@@@@@@@@@@@Dodilation@@@@@@@@@@@@@@@@@@")
                img = dilation(img)
                # plt.imshow(img.detach().squeeze().cpu().numpy())
                # plt.axis('off')
                # plt.savefig("/home/yw2009/anaconda3/envs/medicalimage/cellposes/Gseg_2C_levelset_offset/levelset/img_dilation_{}.png".format(cell_id), bbox_inches='tight', pad_inches = 0 )
                # plt.clf()
            
            LSF = level_set(LSF, img, mu, nu, epison, step)

        LSF[:][LSF[:] >= 0] = 1
        LSF[:][LSF[:] < 0] = 0

        # plt.imshow(LSF.detach().squeeze().cpu().numpy())
        # plt.axis('off')
        # plt.savefig("/home/yw2009/anaconda3/envs/medicalimage/cellposes/Gseg_2C_levelset_offset/levelset/LSF_levelset_{}.png".format(cell_id), bbox_inches='tight', pad_inches = 0 )
        # plt.clf()

        outcoord = torch.nonzero(LSF.squeeze())
        new_mask[outcoord[:,0], outcoord[:,1]] = cell_id

    new_mask = new_mask.detach().squeeze().cpu().numpy()
    # plt.imshow(new_mask)
    # plt.axis('off')
    # plt.savefig("/home/yw2009/anaconda3/envs/medicalimage/cellposes/Gseg_2C_levelset_offset/levelset/new_mask.png", bbox_inches='tight', pad_inches = 0 )
    # plt.clf()
    # print("new_mask:", new_mask.shape)
    # print("new_mask:", np.unique(new_mask))
    return new_mask

def gen_pose_target(joints, device, h=256, w=256, sigma=3):
    #print "Target generation -- Gaussian maps"
    '''
    joints : gene spots #[N,2]
    sigma : 7
    '''
    if joints.shape[0]!=0:
        joint_num = joints.shape[0] #16
        gaussian_maps = torch.zeros((joint_num, h, w)).to(device)

        for ji in range(0, joint_num):
            gaussian_maps[ji, :, :] = gen_single_gaussian_map(joints[ji, :], h, w, sigma, device)

        # Get background heatmap
        max_heatmap = torch.max(gaussian_maps, 0).values #cuda
        # print("max_heatmap:", max_heatmap)
    else:
        max_heatmap = torch.zeros((h, w)).to(device)
    return max_heatmap

def gen_single_gaussian_map(center, h, w, sigma, device):
    #print "Target generation -- Single gaussian maps"
    '''
    center a gene spot #[2,]
    '''

    grid_y, grid_x = torch.meshgrid(torch.arange(h), torch.arange(w), indexing='ij')
    inds = torch.stack([grid_x,grid_y], dim=0).to(device) #[2,256,256]
    d2 = (inds[0] - center[0]) * (inds[0] - center[0]) + (inds[1] - center[1]) * (inds[1] - center[1]) #[256,256]
    exponent = d2 / 2.0 / sigma / sigma #[256,256]
    exp_mask = exponent > 4.6052 #[256,256]
    exponent[exp_mask] = 0
    gaussian_map = torch.exp(-exponent) #[256,256]
    gaussian_map[exp_mask] = 0
    gaussian_map[gaussian_map>1] = 1 #[256,256]

    return gaussian_map

def masks_to_flows_gpu(masks, device=None):
    """ convert masks to flows using diffusion from center pixel
    Center of masks where diffusion starts is defined using COM
    Parameters
    -------------
    masks: int, 2D or 3D array
        labelled masks 0=NO masks; 1,2,...=mask labels
    Returns
    -------------
    offsetmap: 2,h,w
    centermap: h,w
    """
    if device is None:
        device = torch.device('cuda')
    
    Ly0,Lx0 = masks.shape

    # get mask centers
    unique_ids = np.unique(masks)[1:]
    centers = np.zeros((len(unique_ids), 2), 'int')
    offsetmap = np.zeros((2, Ly0, Lx0))
    for i, id in enumerate(unique_ids):
        
        yi,xi = np.nonzero(masks==id)
        yi = yi.astype(np.int32)
        xi = xi.astype(np.int32)
        
        ymed = np.median(yi)
        xmed = np.median(xi)
        imin = np.argmin((xi-xmed)**2 + (yi-ymed)**2)
        
        xmed = xi[imin]
        ymed = yi[imin]
        
        offsetmap[0, yi, xi] = yi - ymed
        offsetmap[1, yi, xi] = xi - xmed
        
        centers[i,0] = xmed
        centers[i,1] = ymed

    centermap = gen_pose_target(centers, device, Ly0, Lx0, 3)
    centermap = centermap.cpu().numpy()

    comap = np.concatenate((offsetmap, centermap[np.newaxis,:,:]), axis=0)
    return comap

def masks_to_flows(masks, use_gpu=False, device=None):
    """ convert masks to flows using diffusion from center pixel

    Center of masks where diffusion starts is defined to be the 
    closest pixel to the median of all pixels that is inside the 
    mask. Result of diffusion is converted into flows by computing
    the gradients of the diffusion density map. 

    Parameters
    -------------

    masks: int, 2D or 3D array
        labelled masks 0=NO masks; 1,2,...=mask labels

    Returns
    -------------

    mu: float, 3D or 4D array 
        flows in Y = mu[-2], flows in X = mu[-1].
        if masks are 3D, flows in Z = mu[0].

    mu_c: float, 2D or 3D array
        for each pixel, the distance to the center of the mask 
        in which it resides 

    """
    # print("dy_masks_to_flows_file:", file)
    if masks.max() == 0:
        dynamics_logger.warning('empty masks!')
        return np.zeros((3, *masks.shape), 'float32')

    if use_gpu:
        if use_gpu and device is None:
            device = torch_GPU
        elif device is None:
            device = torch_CPU
    
    masks_to_flows_device = masks_to_flows_gpu
    if masks.ndim==2:
        comap = masks_to_flows_device(masks, device=device)
        return comap
    else:
        raise ValueError('masks_to_flows only takes 2D')

def labels_to_flows(labels, files=None, use_gpu=False, device=None, redo_flows=False):
    """ convert labels (list of masks or flows) to flows for training model 

    if files is not None, flows are saved to files to be reused

    Parameters
    --------------

    labels: list of ND-arrays
        labels[k] can be 2D or 3D, if [3 x Ly x Lx] then it is assumed that flows were precomputed.
        Otherwise labels[k][0] or labels[k] (if 2D) is used to create flows and cell probabilities.

    Returns
    --------------

    flows: list of [4 x Ly x Lx] arrays
        flows[k][0] is labels[k], flows[k][1] is cell distance transform, flows[k][2] is Y flow,
        flows[k][3] is X flow, and flows[k][4] is heat distribution

    """
    nimg = len(labels)
    if labels[0].ndim < 3:
        labels = [labels[n][np.newaxis,:,:] for n in range(nimg)]

    if labels[0].shape[0] == 1 or labels[0].ndim < 3 or redo_flows: # flows need to be recomputed
        dynamics_logger.info('computing flows for labels')
        
        # compute flows; labels are fixed here to be unique, so they need to be passed back
        # make sure labels are unique!
        labels = [fastremap.renumber(label, in_place=True)[0] for label in labels]
        #print("dy_labels:", np.array(labels).shape) #[2240,1,256,256]
        comap = [masks_to_flows(labels[n][0], use_gpu=use_gpu, device=device) for n in trange(nimg)]
        
        # concatenate labels, distance transform, vector flows, heat (boundary and mask are computed in augmentations)
        flows = [np.concatenate((labels[n], labels[n]>0.5, comap[n]), axis=0).astype(np.float32)
                    for n in range(nimg)]
        if files is not None:
            for flow, file in zip(flows, files):
                file_name = os.path.splitext(file)[0]
                tifffile.imsave(file_name+'_flows.tif', flow)
    else:
        dynamics_logger.info('flows precomputed')
        flows = [labels[n].astype(np.float32) for n in range(nimg)]
    return flows


@njit(['(int16[:,:,:], float32[:], float32[:], float32[:,:])', 
        '(float32[:,:,:], float32[:], float32[:], float32[:,:])'], cache=True)
def map_coordinates(I, yc, xc, Y):
    """
    bilinear interpolation of image 'I' in-place with ycoordinates yc and xcoordinates xc to Y
    
    Parameters
    -------------
    I : C x Ly x Lx
    yc : ni
        new y coordinates
    xc : ni
        new x coordinates
    Y : C x ni
        I sampled at (yc,xc)
    """
    C,Ly,Lx = I.shape
    yc_floor = yc.astype(np.int32)
    xc_floor = xc.astype(np.int32)
    yc = yc - yc_floor
    xc = xc - xc_floor
    for i in range(yc_floor.shape[0]):
        yf = min(Ly-1, max(0, yc_floor[i]))
        xf = min(Lx-1, max(0, xc_floor[i]))
        yf1= min(Ly-1, yf+1)
        xf1= min(Lx-1, xf+1)
        y = yc[i]
        x = xc[i]
        for c in range(C):
            Y[c,i] = (np.float32(I[c, yf, xf]) * (1 - y) * (1 - x) +
                      np.float32(I[c, yf, xf1]) * (1 - y) * x +
                      np.float32(I[c, yf1, xf]) * y * (1 - x) +
                      np.float32(I[c, yf1, xf1]) * y * x )

# def remove_bad_flow_masks(masks, flows, threshold=0.4, use_gpu=False, device=None):
#     """ remove masks which have inconsistent flows 
    
#     Uses metrics.flow_error to compute flows from predicted masks 
#     and compare flows to predicted flows from network. Discards 
#     masks with flow errors greater than the threshold.

#     Parameters
#     ----------------

#     masks: int, 2D or 3D array
#         labelled masks, 0=NO masks; 1,2,...=mask labels,
#         size [Ly x Lx] or [Lz x Ly x Lx]

#     flows: float, 3D or 4D array
#         flows [axis x Ly x Lx] or [axis x Lz x Ly x Lx]

#     threshold: float (optional, default 0.4)
#         masks with flow error greater than threshold are discarded.

#     Returns
#     ---------------

#     masks: int, 2D or 3D array
#         masks with inconsistent flow masks removed, 
#         0=NO masks; 1,2,...=mask labels,
#         size [Ly x Lx] or [Lz x Ly x Lx]
    
#     """
#     merrors, _ = metrics.flow_error(masks, flows, use_gpu, device)
#     badi = 1+(merrors>threshold).nonzero()[0]
#     masks[np.isin(masks, badi)] = 0
#     return masks

def find_center_condidates(centermap, offsetmap, size=[256,256]):
    peak_counter = 1

    heatmap_ori = centermap
    heatmap = gaussian_filter(heatmap_ori, sigma=3)

    heatmap_left = np.zeros(heatmap.shape)
    heatmap_left[1:, :] = heatmap[:-1, :]
    heatmap_right = np.zeros(heatmap.shape)
    heatmap_right[:-1, :] = heatmap[1:, :]
    heatmap_up = np.zeros(heatmap.shape)
    heatmap_up[:, 1:] = heatmap[:, :-1]
    heatmap_down = np.zeros(heatmap.shape)
    heatmap_down[:, :-1] = heatmap[:, 1:]

    peaks_binary = np.logical_and.reduce((heatmap >= heatmap_left, heatmap >= heatmap_right, heatmap >= heatmap_up, heatmap >= heatmap_down, heatmap > 0.1))
    peaks = list(zip(np.nonzero(peaks_binary)[1], np.nonzero(peaks_binary)[0]))
    peaks_with_score = [x + (heatmap_ori[x[1], x[0]], ) for x in peaks]
    id = range(peak_counter, peak_counter + len(peaks))
    peaks_with_score_and_id = [peaks_with_score[i] + (id[i], ) for i in range(len(id))]
    peak_counter = len(peaks)

    # Recover the peaks to locations in original image
    joint_candi_list = []
    for ci in range(0, peak_counter):
        joint_candi = np.zeros((1, 4))
        joint_candi[0, :] = np.array(peaks_with_score_and_id[ci])
        joint_candi_list.append(joint_candi)

    # Get the center embedding results
    embedding_list = []
    for ci in range(0, len(joint_candi_list)):
        joint_candi = joint_candi_list[ci][0, 0:2]
        embedding = np.zeros((1, 2))
        
        g_x = int(joint_candi[0])
        g_y = int(joint_candi[1])
        
        if g_x >= 0 and g_x < size[1] and g_y >= 0 and g_y < size[0]:
            offset_x = offsetmap[0, g_y, g_x]
            offset_y = offsetmap[1, g_y, g_x]
        
            embedding[0, 0] = joint_candi[0] + offset_x
            embedding[0, 1] = joint_candi[1] + offset_y
        embedding_list.append(embedding)
        
    # Convert to np array
    embedding_np_array = np.empty((0, 2))
    for ci in range(0, len(embedding_list)):
        embedding = embedding_list[ci]
        embedding_np_array = np.vstack((embedding_np_array, embedding))

    joint_candi_np_array = np.empty((0, 4))
    for ci in range(0, len(joint_candi_list)):
        joint_candi_with_type = np.zeros((1, 4))
        joint_candi = joint_candi_list[ci]
        joint_candi_with_type[0, :] = joint_candi[0, :]
        joint_candi_np_array = np.vstack((joint_candi_np_array, joint_candi_with_type))

    joint_candi_np_array_withembed = np.empty((0, 4))
    for ci in range(0, len(joint_candi_list)):
        joint_candi_with_type = np.zeros((1, 4))
        joint_candi = joint_candi_list[ci]
        joint_candi_with_type[0, 0:2] = embedding_np_array[ci]
        joint_candi_with_type[0, 2:4] = joint_candi[0, 2:]
        joint_candi_np_array_withembed = np.vstack((joint_candi_np_array_withembed, joint_candi_with_type))

    joint_candi_np_array = joint_candi_np_array[joint_candi_np_array[:,2] > 0.4]
    joint_candi_np_array_withembed = joint_candi_np_array_withembed[joint_candi_np_array_withembed[:,2] > 0.4]
    
    centermap = gen_pose_target(joint_candi_np_array_withembed, torch.device('cuda'))
    # plt.imshow(centermap.cpu().numpy())
    # plt.axis('off')
    # plt.savefig("/home/yw2009/anaconda3/envs/medicalimage/cellposes/Gseg_2C_levelset_offset/images/centermap_pre.png", bbox_inches='tight', pad_inches = 0 )
    # plt.clf()
    return joint_candi_np_array, joint_candi_np_array_withembed

def get_mask(center_coord, offsetmap, cp_mask):
    p_inds = np.meshgrid(np.arange(offsetmap.shape[1]), np.arange(offsetmap.shape[2]), indexing='ij')
    p = np.zeros((2, offsetmap.shape[1], offsetmap.shape[2]))
    for i in range(len(offsetmap)):
        p[i] = p_inds[i] - offsetmap[i]

    Y,X = np.nonzero(cp_mask)
    pre_center_coord = p[:, Y, X][[1,0]].transpose()
    distance_map = np.ones((pre_center_coord.shape[0], center_coord.shape[0]))*np.inf
    for i, cell_center in enumerate(center_coord[:,:2]):
        distance_map[:, i] = np.sqrt(np.sum((pre_center_coord - cell_center.reshape(-1,2))**2, axis=1))
    
    cell_index = np.argmin(distance_map, axis=1)
    mask = np.zeros((offsetmap.shape[1], offsetmap.shape[2]), dtype=np.uint16)
    mask[Y,X] = cell_index+1
    # print("mask:", np.unique(mask))
    # new_label = plot.mask_rgb(mask)
    # cv2.imwrite('/home/yw2009/anaconda3/envs/medicalimage/cellposes/Gseg_2C_levelset_offset/images/mask.png', new_label)
    # plt.imshow(mask)
    # plt.axis('off')
    # plt.savefig("/home/yw2009/anaconda3/envs/medicalimage/cellposes/Gseg_2C_levelset_offset/images/mask_plt.png", bbox_inches='tight', pad_inches = 0 )
    # plt.clf()
    # for id in np.unique(mask)[1:]:
    #     cv2.imwrite('/home/yw2009/anaconda3/envs/medicalimage/cellposes/Gseg_2C_levelset_offset/images/mask_{}.png'.format(id), (mask == id)*255)
    # exit()
    return mask

def compute_masks(offsetmap, centermap, confimap, p=None, 
                   confidence_threshold=0.0,
                   flow_threshold=0.4, interp=True, do_3D=False, 
                   min_size=15, resize=None, 
                   use_gpu=False,device=None):
    """ 
    compute masks using dynamics from offsetmap, confimap, and centermap 
    offsetmap: [2, H, W]
    centermap: [256,256]
    confimap: [256,256]
    """
    # print("confidence_threshold:", confidence_threshold)
    cp_mask = confimap > confidence_threshold 

    if np.any(cp_mask): #mask at this point is a cell cluster binary map, not labels     
        ofmap = offsetmap * cp_mask
        joint_candi_np_array, joint_candi_np_array_withembed = find_center_condidates(centermap, ofmap)
        if len(joint_candi_np_array_withembed) == 0:
            dynamics_logger.info('No cell pixels found.')
            shape = resize if resize is not None else confimap.shape
            mask = np.zeros(shape, np.uint16)
            return mask, p
        
        #calculate masks
        mask = get_mask(joint_candi_np_array_withembed, ofmap, cp_mask)
        
        if resize is not None:
            if mask.max() > 2**16-1:
                recast = True
                mask = mask.astype(np.float32)
            else:
                recast = False
                mask = mask.astype(np.uint16)
            mask = transforms.resize_image(mask, resize[0], resize[1], interpolation=cv2.INTER_NEAREST)
            if recast:
                mask = mask.astype(np.uint32)
        elif mask.max() < 2**16:
            mask = mask.astype(np.uint16)

    else: # nothing to compute, just make it compatible
        dynamics_logger.info('No cell pixels found.')
        shape = resize if resize is not None else confimap.shape
        mask = np.zeros(shape, np.uint16)
        return mask

    # moving the cleanup to the end helps avoid some bugs arising from scaling...
    # maybe better would be to rescale the min_size and hole_size parameters to do the
    # cleanup at the prediction scale, or switch depending on which one is bigger... 
    mask = utils.fill_holes_and_remove_small_masks(mask, min_size=min_size)

    if mask.dtype==np.uint32:
        dynamics_logger.warning('more than 65535 masks in image, masks returned as np.uint32')

    return mask

