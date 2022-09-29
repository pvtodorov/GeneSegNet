import sys, os, argparse, glob, pathlib, time
import subprocess
import cv2
import numpy as np
from natsort import natsorted
from tqdm import tqdm
import utils, metrics, models, Gseg_io, core, plot, dynamics
from pathlib import Path
import fastremap
import csv
import matplotlib.pyplot as plt
from scipy.io import savemat, loadmat
from PIL import Image


    
import logging

# settings re-grouped a bit
parser = argparse.ArgumentParser(description='Gseg parameters')

# settings for CPU vs GPU
hardware_args = parser.add_argument_group("hardware arguments")
hardware_args.add_argument('--use_gpu', action='store_true', help='use gpu if torch with cuda installed')
hardware_args.add_argument('--gpu_device', required=False, default=0, type=int, help='which gpu device to use')
hardware_args.add_argument('--check_mkl', action='store_true', help='check if mkl working')
    
# settings for locating and formatting images
input_img_args = parser.add_argument_group("input image arguments")
input_img_args.add_argument('--train_dir',
                    default=[], type=str, help='folder containing data to run or train on.')
input_img_args.add_argument('--val_dir',
                    default=[], type=str, help='folder containing data to run or val on.')
input_img_args.add_argument('--test_dir',
                    default=[], type=str, help='folder containing test data')
input_img_args.add_argument('--look_one_level_down', action='store_true', help='run processing on all subdirectories of current folder')
input_img_args.add_argument('--img_filter',
                    default='_image', type=str, help='end string for images to run on')
input_img_args.add_argument('--mask_filter',
                    default='_label', type=str, help='end string for masks to run on.')
input_img_args.add_argument('--heatmap_filter',
                    default='_gaumap_all', type=str, help='end string for gaussian map to run on.')
input_img_args.add_argument('--channel_axis',
                    default=None, type=int, help='axis of image which corresponds to image channels')
input_img_args.add_argument('--z_axis',
                    default=None, type=int, help='axis of image which corresponds to Z dimension')
input_img_args.add_argument('--chan',
                    default=0, type=int, help='channel to segment; 0: GRAY, 1: RED, 2: GREEN, 3: BLUE. Default: %(default)s')
input_img_args.add_argument('--chan2',
                    default=0, type=int, help='nuclear channel (if cyto, optional); 0: NONE, 1: RED, 2: GREEN, 3: BLUE. Default: %(default)s')
input_img_args.add_argument('--invert', action='store_true', help='invert grayscale channel')
input_img_args.add_argument('--all_channels', action='store_true', help='use all channels in image if using own model and images with special channels')

# model settings 
model_args = parser.add_argument_group("model arguments")
model_args.add_argument('--pretrained_model', required=False, default=None, type=str, help='model to use for running or starting training')
model_args.add_argument('--unet', action='store_true', help='run standard unet instead of Gseg flow output')
model_args.add_argument('--nclasses', default=4, type=int, help='if running unet, choose 2 or 3;')

# algorithm settings
algorithm_args = parser.add_argument_group("algorithm arguments")
algorithm_args.add_argument('--no_resample', action='store_true', help="disable dynamics on full image (makes algorithm faster for images with large diameters)")
algorithm_args.add_argument('--net_avg', action='store_true', help='run 4 networks instead of 1 and average results')
algorithm_args.add_argument('--no_interp', action='store_true', help='do not interpolate when running dynamics (was default)')
algorithm_args.add_argument('--no_norm', action='store_true', help='do not normalize images (normalize=False)')
algorithm_args.add_argument('--do_3D', action='store_true', help='process images as 3D stacks of images (nplanes x nchan x Ly x Lx')
algorithm_args.add_argument('--diameter', required=False, default=32., type=float, 
                    help='cell diameter, if 0 will use the diameter of the training labels used in the model, or with built-in model will estimate diameter for each image')
algorithm_args.add_argument('--stitch_threshold', required=False, default=0.0, type=float, help='compute masks in 2D then stitch together masks with IoU>0.9 across planes')
algorithm_args.add_argument('--fast_mode', action='store_true', help='now equivalent to --no_resample; make code run faster by turning off resampling')

algorithm_args.add_argument('--flow_threshold', default=0.4, type=float, help='flow error threshold, 0 turns off this optional QC step. Default: %(default)s')
algorithm_args.add_argument('--cellprob_threshold', default=0, type=float, help='cellprob threshold, default is 0, decrease to find more and larger masks')

algorithm_args.add_argument('--anisotropy', required=False, default=1.0, type=float,
                    help='anisotropy of volume in 3D')
algorithm_args.add_argument('--exclude_on_edges', action='store_true', help='discard masks which touch edges of image')

# output settings
output_args = parser.add_argument_group("output arguments")
output_args.add_argument('--save_png', action='store_true', help='save masks as png and outlines as text file for ImageJ')
output_args.add_argument('--save_tif', action='store_true', help='save masks as tif and outlines as text file for ImageJ')
output_args.add_argument('--no_npy', action='store_true', help='suppress saving of npy')
output_args.add_argument('--savedir',
                    default=None, type=str, help='folder to which segmentation results will be saved (defaults to input image directory)')
output_args.add_argument('--output_filename', default="newlabels", type=str, help='output filename') 
output_args.add_argument('--dir_above', action='store_true', help='save output folders adjacent to image folder instead of inside it (off by default)')
output_args.add_argument('--in_folders', action='store_true', help='flag to save output in folders (off by default)')
output_args.add_argument('--save_flows', action='store_true', help='whether or not to save RGB images of flows when masks are saved (disabled by default)')
output_args.add_argument('--save_outlines', action='store_true', help='whether or not to save RGB outline images when masks are saved (disabled by default)')
output_args.add_argument('--save_ncolor', action='store_true', help='whether or not to save minimal "n-color" masks (disabled by default')
output_args.add_argument('--save_txt', action='store_true', help='flag to enable txt outlines for ImageJ (disabled by default)')
output_args.add_argument('--metrics', action='store_true', help='compute the segmentation metrics')
output_args.add_argument('--save_model_dir', default=[], type=str, help='save trained model')
#output_args.add_argument('--variance', type=int, default=3)

# training settings
training_args = parser.add_argument_group("training arguments")
training_args.add_argument('--train', action='store_true', help='train network using images in dir')
training_args.add_argument('--train_size', action='store_true', help='train size network at end of training')
# training_args.add_argument('--test_dir',
#                     default=[], type=str, help='folder containing test data (optional)')
# training_args.add_argument('--mask_filter',
#                     default='_masks', type=str, help='end string for masks to run on. Default: %(default)s')
training_args.add_argument('--diam_mean',
                    default=34., type=float, help='mean diameter to resize cells to during training -- if starting from pretrained models it cannot be changed from 30.0')
training_args.add_argument('--learning_rate',
                    default=0.1, type=float, help='learning rate. Default: %(default)s')
training_args.add_argument('--weight_decay',
                    default=0.00001, type=float, help='weight decay. Default: %(default)s')
training_args.add_argument('--n_epochs',
                    default=100, type=int, help='number of epochs. Default: %(default)s')
training_args.add_argument('--batch_size',
                    default=8, type=int, help='batch size. Default: %(default)s')
training_args.add_argument('--min_train_masks',
                    default=2, type=int, help='minimum number of masks a training image must have to be used. Default: %(default)s')
training_args.add_argument('--residual_on',
                    default=1, type=int, help='use residual connections')
training_args.add_argument('--style_on',
                    default=1, type=int, help='use style vector')
training_args.add_argument('--concatenation',
                    default=0, type=int, help='concatenate downsampled layers with upsampled layers (off by default which means they are added)')
training_args.add_argument('--save_every',
                    default=100, type=int, help='number of epochs to skip between saves. Default: %(default)s')
training_args.add_argument('--save_each', action='store_true', help='save the model under a different filename per --save_every epoch for later comparsion')
training_args.add_argument('--variance', type=int, default=3)
# training_args.add_argument('--gauAll', action='store_true')

# misc settings
parser.add_argument('--verbose', action='store_true', help='show information about running and settings and save to log')

def label_ppreprocess(args, logger, N):            
    device, gpu = models.assign_device(use_torch=True, gpu=args.use_gpu, device=args.gpu_device)

    if args.pretrained_model is None or args.pretrained_model == 'None' or args.pretrained_model == 'False' or args.pretrained_model == '0':
        pretrained_model = False
    else:
        pretrained_model = args.pretrained_model
    
    inference_list = [args.train_dir, args.val_dir]
    
    logger.info('>>>> start post-preprocess')

    for infer in inference_list:
        tic = time.time()
        output = Gseg_io.load_train_test_data(infer, N, image_filter = args.img_filter, 
                                        mask_filter = args.mask_filter, heatmap_filter = args.heatmap_filter, 
                                        foldername = args.output_filename, variance = args.variance)

        images, labels, heatmaps, spots, label_names,_,_,_,_,_ = output
        images = list(np.stack((images, heatmaps), axis=3))

        nimg = len(label_names)
        logger.info('>>>> running Gseg on %d images'% nimg)
                
        if args.all_channels:
            nchan = 2
            channels = None 
        else:
            nchan = 2
            
        model = models.CellposeModel(gpu=gpu, device=device, 
                                        pretrained_model=pretrained_model,
                                        model_type=None,
                                        diam_mean=args.diam_mean,
                                        residual_on=args.residual_on,
                                        style_on=args.style_on,
                                        concatenation=args.concatenation,
                                        net_avg=False,
                                        nchan=nchan)

        diameter = args.diameter
        # logger.info('>>>> using diameter %0.3f for all images'%diameter)

        # tqdm_out = utils.TqdmToLogger(logger,level=logging.INFO)
        assert len(images) == len(labels) == len(spots) == len(label_names)
        for image, label, spot, label_name in zip(images, labels, spots, label_names):
            out = model.eval(image, channels=channels, diameter=diameter,
                            do_3D=args.do_3D, net_avg=(not args.fast_mode or args.net_avg),
                            augment=False,
                            resample=(not args.no_resample and not args.fast_mode),
                            flow_threshold=args.flow_threshold,
                            cellprob_threshold=args.cellprob_threshold,
                            stitch_threshold=args.stitch_threshold,
                            invert=args.invert,
                            batch_size=args.batch_size,
                            interp=(not args.no_interp),
                            normalize=(not args.no_norm),
                            channel_axis=args.channel_axis,
                            z_axis=args.z_axis,
                            anisotropy=args.anisotropy,
                            model_loaded=True)
            masks, flows = out[:2]
            
            #post-preprocess:
            savedir = str(Path(label_name).parent.parent.absolute()) + '/{}'.format(args.output_filename)
            basename = os.path.splitext(os.path.basename(label_name))[0]
            
            if os.path.exists(savedir):
                if os.path.exists('{}/{}_flows.tif'.format(savedir, basename)):
                    os.remove('{}/{}_flows.tif'.format(savedir, basename))
            
            if not os.path.exists(savedir):
                os.makedirs(savedir)

            mask = dynamics.postprocess(masks, N, device = device)

            im = Image.fromarray(mask)
            im_path = os.path.join(savedir, basename + '.png')
            im.save(im_path)

    logger.info('>>>> finish post-preprocess in %0.3f sec'%(time.time()-tic))

def test(args, logger, N):
    logger.info('>>>> start test')
    saving_something = args.save_png or args.save_tif or args.save_flows or args.save_ncolor or args.save_txt      
    device, gpu = models.assign_device(use_torch=True, gpu=args.use_gpu, device=args.gpu_device)

    if args.pretrained_model is None or args.pretrained_model == 'None' or args.pretrained_model == 'False' or args.pretrained_model == '0':
        pretrained_model = False
    else:
        pretrained_model = args.pretrained_model
    
    # print("pretrained_model:",pretrained_model) #False
    
    tic = time.time()
    output = Gseg_io.load_train_test_data(args.test_dir, N, image_filter = args.img_filter, 
                                    mask_filter = args.mask_filter, heatmap_filter = args.heatmap_filter, 
                                    foldername = args.output_filename, variance = args.variance)

    images, labels, heatmaps, spots, label_names,_,_,_,_,_ = output
    images = list(np.stack((images, heatmaps), axis=3))

    nimg = len(label_names)
    logger.info('>>>> running Gseg on %d images'% nimg)  

    if args.all_channels:
        nchan = 2
        channels = None 
    else:
        nchan = 2

    model = models.CellposeModel(gpu=gpu, device=device, 
                                    pretrained_model=pretrained_model,
                                    model_type=None,
                                    diam_mean=args.diam_mean,
                                    residual_on=args.residual_on,
                                    style_on=args.style_on,
                                    concatenation=args.concatenation,
                                    net_avg=False,
                                    nchan=nchan)
    diameter = args.diameter
    # logger.info('>>>> using diameter %0.3f for all images'%diameter)

    filename = []
    masks_true = []
    masks_pred = []
    #tqdm_out = utils.TqdmToLogger(logger,level=logging.INFO)

    logger.info('>>>> compute IoU and save predicted results')
    assert len(images) == len(labels) == len(spots) == len(label_names)
    #for image, label, spot, label_name in tqdm(zip(images, labels, spots, label_names), file=tqdm_out):
    for image, label, spot, label_name in zip(images, labels, spots, label_names):
        out = model.eval(image, channels=channels, diameter=diameter,
                        do_3D=args.do_3D, net_avg=(not args.fast_mode or args.net_avg),
                        augment=False,
                        resample=(not args.no_resample and not args.fast_mode),
                        flow_threshold=args.flow_threshold,
                        cellprob_threshold=args.cellprob_threshold,
                        stitch_threshold=args.stitch_threshold,
                        invert=args.invert,
                        batch_size=args.batch_size,
                        interp=(not args.no_interp),
                        normalize=(not args.no_norm),
                        channel_axis=args.channel_axis,
                        z_axis=args.z_axis,
                        anisotropy=args.anisotropy,
                        model_loaded=True)
        masks, flows = out[:2]

        if len(out) > 3:
            diams = out[-1]
        else:
            diams = diameter
        if args.exclude_on_edges:
            masks = utils.remove_edge_masks(masks)
        if args.no_npy:
            Gseg_io.masks_flows_to_seg(image[:,:,0], masks, flows, diams, label_name, channels)
        if saving_something:
            Gseg_io.save_masks(image[:,:,0], masks, flows, label, spot, label_name, args.variance, png=args.save_png, tif=args.save_tif,
                            foldername = args.output_filename, save_flows=args.save_flows,save_outlines=args.save_outlines,
                            save_ncolor=args.save_ncolor,dir_above=args.dir_above,savedir=args.savedir,
                            save_txt=args.save_txt, in_folders=args.in_folders)
        
        if label.max() != 0:
            filename.append(os.path.basename(label_name))
            masks_true.append(label)
            masks_pred.append(masks)
        
    if args.metrics:
        ap, tp, fp, fn = metrics.average_precision(masks_true, masks_pred)    
        print("average precision: %0.3f at threshold 0.5, %0.3f at threshold 0.75 and %0.3f at threshold 0.9"%(np.mean(ap[:,0]), np.mean(ap[:,1]), np.mean(ap[:,2])))

    iou = metrics.compute_IoU(masks_pred, args.test_dir)
    print("mIoU: %0.3f"%(iou))

    logger.info('>>>> finish text in %0.3f sec'%(time.time()-tic))
    return np.mean(ap[:,0])

def train(args, logger, N):
    device, gpu = models.assign_device(use_torch=True, gpu=args.use_gpu, device=args.gpu_device)

    # if args.pretrained_model is None or args.pretrained_model == 'None' or args.pretrained_model == 'False' or args.pretrained_model == '0':
    #     pretrained_model = False
    # else:
    #     pretrained_model = args.pretrained_model

    args.pretrained_model = False
    pretrained_model = args.pretrained_model

    val_dir = None if len(args.val_dir)==0 else args.val_dir

    output = Gseg_io.load_train_test_data(args.train_dir, N, val_dir, image_filter = args.img_filter, 
                                    mask_filter = args.mask_filter, heatmap_filter = args.heatmap_filter, 
                                    foldername = args.output_filename, variance = args.variance)
    images, labels, heatmaps, spots ,label_names, test_images, test_labels, test_heatmaps, test_spots, test_label_names = output

    # print("images:", images[0].max(), images[0].min()) #[0,1]
    # print("labels:", labels[0].max(), labels[0].min())
    # print("heatmaps:", heatmaps[0].max(), heatmaps[0].min()) #[0,1]

    # print("images_b:",np.array(images).shape) #(2100, 256, 256)
    # print("labels_b:",np.array(labels).shape) #(2100, 256, 256)
    # print("gaumaps_b:",np.array(heatmaps).shape) #(2100, 256, 256)
    # print("spots_b:", len(spots)) #2100
    # print("test_images_b:",np.array(test_images).shape)#(420, 256, 256)
    # print("test_labels_b:",np.array(test_labels).shape)#(420, 256, 256)
    # print("test_gaumaps_b:",np.array(test_heatmaps).shape)#(420, 256, 256)
    # print("test_spots_b:", len(test_spots)) #420

    assert len(images) == len(labels) == len(heatmaps) == len(label_names) == len(spots)
    assert len(test_images) == len(test_labels) == len(test_heatmaps) == len(test_label_names) == len(test_spots)

    images = list(np.stack((images, heatmaps), axis=3))
    test_images = list(np.stack((test_images, test_heatmaps), axis=3))

    # print("images:",np.array(images).shape) #(2239,256,256,2)
    # print("labels:",np.array(labels).shape) #(2239, 4,256,256)
    # print("test_images:",np.array(test_images).shape) #(560, 256, 256, 2)
    # print("test_labels:",np.array(test_labels).shape) #(560, 4, 256, 256)

    # training with all channels
    if args.all_channels:
        img = images[0]
        if img.ndim==3:
            nchan = 2
        elif img.ndim==2:
            nchan = 1
        channels = None 
    else:
        nchan = 2 

    logger.info('>>>> during training rescaling images to fixed diameter of %0.1f pixels'%args.diam_mean)
    logger.info('>>>> start training')    
    # initialize model
    model = models.CellposeModel(device=device,
                                pretrained_model=pretrained_model,
                                model_type=None, 
                                diam_mean=args.diam_mean,
                                residual_on=args.residual_on,
                                style_on=args.style_on,
                                concatenation=args.concatenation,
                                nchan=nchan)
    
    # train segmentation model
    cpmodel_path = model.train(images, labels, train_files=label_names,
                                test_data=test_images, test_labels=test_labels, test_files=test_label_names,
                                learning_rate=args.learning_rate, 
                                weight_decay=args.weight_decay,
                                channels=channels,
                                save_path=os.path.realpath(args.save_model_dir), save_every=args.save_every,
                                save_each=args.save_each,
                                n_epochs=args.n_epochs,
                                batch_size=args.batch_size, 
                                min_train_masks=args.min_train_masks)
    model.pretrained_model = cpmodel_path
    args.pretrained_model = cpmodel_path
    logger.info('>>>> model trained and saved to %s'%cpmodel_path)

if __name__ == '__main__':
    args = parser.parse_args()
    # print("args:", args)
    if args.verbose:
        from Gseg_io import logger_setup
        logger, log_file = logger_setup()
    else:
        print('>>>> !NEW LOGGING SETUP! To see Gseg progress, set --verbose')
        print('No --verbose => no progress or info printed')
        logger = logging.getLogger(__name__)

    IoU = 0
    N = 1
    while IoU <= 0.95:
        logger.info('>>>> %dth external iteration'% N)
        train(args, logger, N)
        IoU = test(args, logger, N)
        if IoU<=0.95:
            label_ppreprocess(args, logger, N)
        N += 1
    logger.info('>>>> finsh training')

# if __name__ == '__main__':
#     args = parser.parse_args()
#     # print("args:", args)
#     if args.verbose:
#         from Gseg_io import logger_setup
#         logger, log_file = logger_setup()
#     else:
#         print('>>>> !NEW LOGGING SETUP! To see Gseg progress, set --verbose')
#         print('No --verbose => no progress or info printed')
#         logger = logging.getLogger(__name__)

#     N = 1
#     IoU = test(args, logger, N)
#     # label_ppreprocess(args, logger, N)
    
#     logger.info('>>>> finsh training')

    
