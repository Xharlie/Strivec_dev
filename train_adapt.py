import os
from tqdm.auto import tqdm
from opt_adapt import config_parser
args = config_parser()
print(args)
os.environ["CUDA_VISIBLE_DEVICES"]=args.gpu_ids
from models.apparatus import *
from preprocessing.recon_prior_adapt import gen_geo, gen_pnts
import json, random
from renderer import *
from utils import *
from torch.utils.tensorboard import SummaryWriter
import datetime
from dataLoader import dataset_dict
import sys
from models.masked_adam import MaskedAdam
from models.init_net.run import get_density_pnts
from sklearn.decomposition import PCA
import pickle
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

renderer = OctreeRender_trilinear_fast
from dataLoader.ray_utils import SimpleSampler

@torch.no_grad()
def export_mesh(args, cluster_dict):

    ckpt = torch.load(args.ckpt, map_location=device)
    kwargs = ckpt['kwargs']
    kwargs.update({'device': device})
    kwargs.update({'geo': cluster_dict["cluster_xyz"], "args":args, "local_dims":args.local_dims_final})
    tensorf = eval(args.model_name)(**kwargs)
    tensorf.load(ckpt)

    alpha,_ = tensorf.getDenseAlpha()
    convert_sdf_samples_to_ply(alpha.cpu(), f'{args.ckpt[:-3]}.ply',bbox=tensorf.aabb.cpu(), level=0.005)


@torch.no_grad()
def render_test(args, test_dataset):
    # init dataset
    white_bg = test_dataset.white_bg
    ray_type = args.ray_type

    if not os.path.exists(args.ckpt):
        print('the ckpt path does not exists!!')
        return

    ckpt = torch.load(args.ckpt, map_location=device)
    with open(args.info_ckpt, 'rb') as f:
        info = pickle.load(f)
    kwargs = ckpt['kwargs']
    kwargs.update({'device': device})
    kwargs.update({'step_ratio': args.step_ratio, "args":args})
    tensorf = eval(args.model_name)(**kwargs)
    tensorf.load(ckpt, info)
    logfolder = os.path.dirname(args.ckpt)
    
    # render_only=0, render_path=0, render_test=1, render_train=0

    os.makedirs(f'{logfolder}/{args.expname}/imgs_test_all', exist_ok=True)
    evaluation(test_dataset,tensorf, args, renderer, f'{logfolder}/{args.expname}/imgs_test_all/', N_vis=-1, N_samples=-1, white_bg = white_bg, ray_type=ray_type,device=device)

    if args.render_path:
        c2ws = test_dataset.render_path
        os.makedirs(f'{logfolder}/{args.expname}/imgs_path_all', exist_ok=True)
        evaluation_path(test_dataset,tensorf, c2ws, renderer, f'{logfolder}/{args.expname}/imgs_path_all/',
                                N_vis=-1, N_samples=-1, white_bg = white_bg, ray_type=ray_type,device=device)


def reconstruction(args, cluster_dict, train_dataset, test_dataset, pnts):
    # init dataset

    # if cluster_dict is None:
    #     if hasattr(train_dataset, "center"):
    #         geo = [train_dataset.center.reshape(1,3)]
    #     else:
    #         geo = [torch.zeros([1,3], device="cuda", dtype=torch.float32)]
    white_bg = train_dataset.white_bg
    near_far = train_dataset.near_far
    ray_type = args.ray_type

    # init resolution
    update_AlphaMask_list = args.update_AlphaMask_list

    if args.add_timestamp:
        logfolder = f'{args.basedir}/{args.expname}{datetime.datetime.now().strftime("-%Y%m%d-%H%M%S")}'
    else:
        logfolder = f'{args.basedir}/{args.expname}'

    # init log file
    os.makedirs(logfolder, exist_ok=True)
    os.makedirs(f'{logfolder}/imgs_vis', exist_ok=True)
    os.makedirs(f'{logfolder}/imgs_rgba', exist_ok=True)
    os.makedirs(f'{logfolder}/rgba', exist_ok=True)

    # init parameters
    aabb = train_dataset.scene_bbox.to(device)
    print("aabb", aabb)
    if args.ckpt is not None:
        ckpt = torch.load(args.ckpt, map_location=device)
        kwargs = ckpt['kwargs']
        kwargs.update({'device':device, "cluster_dict": cluster_dict, "local_dims":args.local_dims_final, "pnts":pnts})
        tensorf = eval(args.model_name)(**kwargs)
        tensorf.load(ckpt)
    else:
        tensorf = eval(args.model_name)(aabb, None, device,
            density_n_comp=args.n_lamb_sigma, appearance_n_comp=args.n_lamb_sh,
            app_dim=args.data_dim_color, near_far=near_far, shadingMode=args.shadingMode,
            alphaMask_thres=args.alpha_mask_thre, density_shift=args.density_shift,
            distance_scale=args.distance_scale, pos_pe=args.pos_pe, view_pe=args.view_pe,
            fea_pe=args.fea_pe, featureC=args.featureC, step_ratio=args.step_ratio,
            fea2denseAct=args.fea2denseAct, local_dims=args.local_dims_init, cluster_dict=cluster_dict, pnts=pnts, args=args)

    # init grad, optimizer and lr

    skip_zero_grad = args.skip_zero_grad
    grad_vars = tensorf.get_optparam_groups(args.lr_init, args.lr_basis, skip_zero_grad = skip_zero_grad > 0)
    if args.lr_decay_iters > 0:
        lr_factor = args.lr_decay_target_ratio**(1/args.lr_decay_iters)
    else:
        args.lr_decay_iters = args.n_iters
        lr_factor = args.lr_decay_target_ratio**(1/args.n_iters)

    print("lr decay", args.lr_decay_target_ratio, args.lr_decay_iters)
    
    optimizer = MaskedAdam(grad_vars, betas=(0.9,0.99)) if skip_zero_grad else torch.optim.Adam(grad_vars, betas=(0.9,0.99))

    # TODO optimize rotation with gradients
    if args.rotgrad > 0:
        geo_optimizer = torch.optim.Adam(tensorf.get_geoparam_groups(args.lr_geo_init), betas=(0.9, 0.99)) # to optimize rotation with gradient

    dim_lst = []

    # set upsample voxel dims
    if args.local_dims_trend is not None: # 1
        assert args.upsamp_list is not None and len(args.upsamp_list) == len(args.local_dims_trend[0]), "args.local_dims_trend and args.upsamp_list mismatch "
        for i in range(len(args.local_dims_trend)):
            level_dim_lst = []
            trend = torch.as_tensor(args.local_dims_trend[i], device="cuda")
            for j in range(len(args.local_dims_init[i])):
                level_dim_lst.append(torch.floor(trend * args.local_dims_final[i][j] / args.local_dims_final[i][0]).long())
            dim_lst.append(torch.stack(level_dim_lst, dim=-1))

    else:
        print("no implementation!!!")
        exit()

    torch.cuda.empty_cache()
    PSNRs,PSNRs_test = [],[0]

    # gather rays

    allrays, allrgbs = train_dataset.all_rays, train_dataset.all_rgbs
   
    if args.ray_type != 1: # if 2, inward facing; if 1, outward facing
        mask_filtered, tensoRF_per_ray = tensorf.filtering_rays(allrays, allrgbs, bbox_only=True)
        allrays, allrgbs = allrays[mask_filtered], allrgbs[mask_filtered]
    trainingSampler = SimpleSampler(allrays.shape[0], args.batch_size)

    # set loss for tensor constraint
    Ortho_reg_weight = args.Ortho_weight
    print("initial Ortho_reg_weight", Ortho_reg_weight)

    L1_reg_weight = args.L1_weight_inital
    print("initial L1_reg_weight", L1_reg_weight)
    TV_weight_density, TV_weight_app = args.TV_weight_density, args.TV_weight_app
    tvreg = TVLoss()
    print(f"initial TV_weight density: {TV_weight_density} appearance: {TV_weight_app}")
   

    pbar = tqdm(range(args.n_iters), miniters=args.progress_refresh_rate, file=sys.stdout)

    # set up epoch for shrink, alphamask, upsample
    shrink_list = [update_AlphaMask_list[0]] if args.shrink_list is None else args.shrink_list
    filter_ray_list = [update_AlphaMask_list[1]] if args.filter_ray_list is None else args.filter_ray_list
    new_aabb = None
    cur_rot_step = False
    rot_step = args.rot_step
    upsamp_reset_list = args.upsamp_reset_list if args.upsamp_reset_list is not None else [0 for i in range(len(args.upsamp_list))]
    up_stage=0 # stage of upsampling the tensorf grid
    adapt_lvl = 0
    for iteration in pbar:
        ray_idx = trainingSampler.nextids()
        rays_train, rgb_train, tensoRF_per_ray_train = allrays[ray_idx].to(device), allrgbs[ray_idx].to(device), None if tensoRF_per_ray is None else tensoRF_per_ray[ray_idx].to(device)
        # TODO if use gradients to optimze rotation
        if args.rotgrad > 0 and rot_step is not None and iteration in rot_step:
            cur_rot_step = not cur_rot_step
            rot_step.pop(0)
            if not cur_rot_step:
                draw_box(tensorf.pnt_xyz, args.local_range, logfolder, iteration, rot_m=tensorf.rot2m(tensorf.pnt_rot))
                tensorf.max_tensoRF = args.max_tensoRF
                tensorf.K_tensoRF = args.K_tensoRF
                tensorf.KNN = args.KNN > 0
            else:
                tensorf.max_tensoRF = args.rot_max_tensoRF if args.rot_max_tensoRF is not None else args.max_tensoRF
                tensorf.KNN = (args.rot_KNN  > 0) if args.rot_KNN is not None else (args.KNN > 0)
                tensorf.K_tensoRF = args.rot_K_tensoRF if args.rot_K_tensoRF is not None else args.K_tensoRF
            tensorf.K_tensoRF = tensorf.max_tensoRF if tensorf.K_tensoRF is None else tensorf.K_tensoRF
            print("rot_step switch to ", cur_rot_step, "; KNN:", tensorf.KNN > 0, ";Query", tensorf.K_tensoRF, "/", tensorf.max_tensoRF)

        # intput ray and do ray marching: get rgb_map, alphas_map, depth_map, weights, uncertainty
        rgb_map, weights, depth_map, rgbpers, ray_ids = renderer(rays_train, tensorf, chunk=args.batch_size, N_samples=-1, white_bg = white_bg, ray_type=ray_type, device=device, is_train=True, tensoRF_per_ray=tensoRF_per_ray_train, rot_step=cur_rot_step)

        loss = torch.mean((rgb_map - rgb_train) ** 2)

        # loss
        total_loss = loss
        if Ortho_reg_weight > 0:
            loss_reg = tensorf.vector_comp_diffs()
            total_loss += Ortho_reg_weight*loss_reg
            # summary_writer.add_scalar('train/reg', loss_reg.detach().item(), global_step=iteration)
        if L1_reg_weight > 0:
            loss_reg_L1 = tensorf.density_L1()
            total_loss += L1_reg_weight*loss_reg_L1
            # summary_writer.add_scalar('train/reg_l1', loss_reg_L1.detach().item(), global_step=iteration)

        if TV_weight_density>0:
            TV_weight_density *= lr_factor
            loss_tv = tensorf.TV_loss_density(tvreg) * TV_weight_density
            total_loss = total_loss + loss_tv
            # summary_writer.add_scalar('train/reg_tv_density', loss_tv.detach().item(), global_step=iteration)
        if TV_weight_app>0:
            TV_weight_app *= lr_factor
            loss_tv = loss_tv + tensorf.TV_loss_app(tvreg)*TV_weight_app
            total_loss = total_loss + loss_tv
            # summary_writer.add_scalar('train/reg_tv_app', loss_tv.detach().item(), global_step=iteration)
        if args.weight_rgbper > 0:
            total_loss += args.weight_rgbper * ((rgbpers - rgb_train[ray_ids]).pow(2).sum(-1) * weights.detach()).sum() / len(rgb_train)
            # summary_writer.add_scalar('train/rgbper', loss_reg_L1.detach().item(), global_step=iteration)
        # if not rot_step:
        optimizer.zero_grad(set_to_none=True) if skip_zero_grad else optimizer.zero_grad()
        if cur_rot_step:
            geo_optimizer.zero_grad()
        total_loss.backward()
        # print("tensorf.basis_mat[0]", cur_rot_step, tensorf.density_line[0].grad)
        # if not rot_step:
        optimizer.step()
        if cur_rot_step:
            geo_optimizer.step()


        loss = loss.detach().item()
        
        PSNRs.append(-10.0 * np.log(loss) / np.log(10.0))
        # summary_writer.add_scalar('train/PSNR', PSNRs[-1], global_step=iteration)
        # summary_writer.add_scalar('train/mse', loss, global_step=iteration)


        for param_group in optimizer.param_groups:
            param_group['lr'] = param_group['lr'] * lr_factor

        # Print the current values of the losses.
        if iteration % args.progress_refresh_rate == 0:
            pbar.set_description(
                f'Iteration {iteration:05d}:'
                + f' train_psnr = {float(np.mean(PSNRs)):.2f}'
                + f' test_psnr = {float(np.mean(PSNRs_test)):.2f}'
                + f' mse = {loss:.6f}'
                #+ (f' rotx = {tensorf.pnt_rot[0,0] * 180 / np.pi:.6f}' if args.rotgrad > 0 else "")
                #+ (f' roty = {tensorf.pnt_rot[0,1] * 180 / np.pi:.6f}' if args.rotgrad > 0 else "")
                #+ (f' rotz = {tensorf.pnt_rot[0,2] * 180 / np.pi:.6f}' if args.rotgrad > 0 else "")
                #+ (f' rotx = {tensorf.pnt_rot[0][0,0].cpu().numpy() * 180 / np.pi:.6f}')
                #+ (f' roty = {tensorf.pnt_rot[0][0,1].cpu().numpy() * 180 / np.pi:.6f}')
                #+ (f' rotz = {tensorf.pnt_rot[0][0,2].cpu().numpy() * 180 / np.pi:.6f}')
            )
            PSNRs = []

        # visualize every $vis_every iters
        if iteration % args.vis_every == args.vis_every - 1 and args.N_vis!=0:
            # test_dataset
            PSNRs_test = evaluation(test_dataset, tensorf, args, renderer, f'{logfolder}/imgs_vis/', N_vis=args.N_vis, prtx=f'{iteration:06d}_', N_samples=-1, white_bg = white_bg, ray_type=ray_type, compute_extra_metrics=False)
            # summary_writer.add_scalar('test/psnr', np.mean(PSNRs_test), global_step=iteration)

        # update alpha mask -> shrink scene box -> filter rays -> adaptive -> upsample tensorf grid ->
        if update_AlphaMask_list is not None and iteration in update_AlphaMask_list:
            new_aabb = tensorf.updateAlphaMask()

        if iteration in shrink_list:
            assert new_aabb is not None, "can't shrink before first updateAlphaMask"
            tensorf.shrink(new_aabb)
            L1_reg_weight = args.L1_weight_rest
            print("continuing L1_reg_weight", L1_reg_weight)

        if args.ray_type != 1 and iteration in filter_ray_list:
            # filter rays outside the bbox
            mask_filtered, tensoRF_per_ray = tensorf.filtering_rays(allrays, allrgbs)
            tensoRF_per_ray = None if tensoRF_per_ray is None else tensoRF_per_ray.to(device)
            allrays, allrgbs = allrays[mask_filtered], allrgbs[mask_filtered]
            trainingSampler = SimpleSampler(allrgbs.shape[0], args.batch_size)

        # TODO adaptively adding new tensoRF
        # if args.adapt_list is not None and iteration in args.adapt_list:
        #     find_shadingloss(geo, train_dataset, allrays, allrgbs, tensorf, args, renderer, white_bg, ray_type, device, num_top_rays=args.top_rays[adapt_lvl])
        #     # tensorf.adapt_add(adapt_lvl)
        #     adapt_lvl += 1
            
        if args.upsamp_list is not None and iteration in args.upsamp_list:
            up_stage+=1
            reset = upsamp_reset_list.pop(0) > 0
            tensorf.up_stage = up_stage
            tensorf.upsample_volume_grid(reset_feat=reset)

            if args.lr_upsample_reset:
                print("reset lr to initial")
                lr_scale = 1 #0.1 ** (iteration / args.n_iters)
            else:
                lr_scale = args.lr_decay_target_ratio ** (iteration / args.n_iters)
            grad_vars = tensorf.get_optparam_groups(args.lr_init*lr_scale, args.lr_basis*lr_scale, skip_zero_grad = skip_zero_grad > 0)
            optimizer = MaskedAdam(grad_vars, betas=(0.9,0.99)) if skip_zero_grad else torch.optim.Adam(grad_vars, betas=(0.9,0.99))

            # TODO use grads to optimize tensorf rotation
            if args.rotgrad > 0:
                geo_optimizer = torch.optim.Adam(tensorf.get_geoparam_groups(args.lr_geo_init * lr_scale), betas=(0.9,0.99), weight_decay=0.0)

        if args.rmv_unused_list is not None and iteration in args.rmv_unused_list:
            tensorf.filtering_tensorf(allrays, iteration=iteration)
            lr_scale = args.lr_decay_target_ratio ** (iteration / args.n_iters)
            grad_vars = tensorf.get_optparam_groups(args.lr_init*lr_scale, args.lr_basis*lr_scale, skip_zero_grad = skip_zero_grad > 0)
            optimizer = MaskedAdam(grad_vars, betas=(0.9,0.99)) if skip_zero_grad else torch.optim.Adam(grad_vars, betas=(0.9,0.99))

    tensorf.save(f'{logfolder}/{args.expname}')

    # test after training
    if args.render_train:
        os.makedirs(f'{logfolder}/imgs_train_all', exist_ok=True)
        train_dataset = dataset(args.datadir, split='train', downsample=args.downsample_train, is_stack=True)
        PSNRs_test = evaluation(train_dataset, tensorf, args, renderer, f'{logfolder}/imgs_train_all/', N_vis=-1, N_samples=-1, white_bg = white_bg, ray_type=ray_type,device=device)
        print(f'======> {args.expname} test all psnr: {np.mean(PSNRs_test)} <========================')

    if args.render_test:
        os.makedirs(f'{logfolder}/imgs_test_all', exist_ok=True)
        PSNRs_test = evaluation(test_dataset, tensorf, args, renderer, f'{logfolder}/imgs_test_all/', N_vis=-1, N_samples=-1, white_bg = white_bg, ray_type=ray_type,device=device)
        # summary_writer.add_scalar('test/psnr_all', np.mean(PSNRs_test), global_step=iteration)
        print(f'======> {args.expname} test all psnr: {np.mean(PSNRs_test)} <========================')

    if args.render_path:
        c2ws = test_dataset.render_path
        # c2ws = test_dataset.poses
        print('========>',c2ws.shape)
        os.makedirs(f'{logfolder}/imgs_path_all', exist_ok=True)
        evaluation_path(test_dataset, tensorf, c2ws, renderer, f'{logfolder}/imgs_path_all/', N_vis=-1, N_samples=-1, white_bg = white_bg, ray_type=ray_type,device=device)


def add_dim(obj, times, div=False):
    if obj is None:
        return obj
    elif div:
        obj_lst = []
        for j in range(times):
            leng = len(obj) // times
            obj_lst.append([obj[i] for i in range(j*leng, j*leng+leng)])
        return obj_lst
    else:
        assert len(obj) % times == 0, "{} should be times of {}".format(obj, times)
        obj_lst = []
        for j in range(len(obj) // times):
            obj_lst.append([obj[j*times+i] for i in range(times)])
        return obj_lst


def comp_revise(args):
    args.local_dims_trend = add_dim(args.local_dims_trend, len(args.max_tensoRF), div=True)
    args.local_range = add_dim(args.local_range, 3)
    args.local_dims_init = add_dim(args.local_dims_init, 3)
    args.local_dims_final = add_dim(args.local_dims_final, 3)
    args.n_lamb_sigma = add_dim(args.n_lamb_sigma, 1)
    args.n_lamb_sh = add_dim(args.n_lamb_sh, 1)
    args.vox_range = add_dim(args.vox_range, 3)
    print("local_dims_trend", args.local_dims_trend)
    print("local_range", args.local_range)
    print("local_dims_init", args.local_dims_init)
    print("local_dims_final", args.local_dims_final)
    print("n_lamb_sigma", args.n_lamb_sigma)
    print("n_lamb_sh", args.n_lamb_sh)
    print("vox_range", args.vox_range)
    args.local_unit = add_dim(args.local_unit, 1 + len(args.upsamp_list))
    return args

@torch.no_grad()
def find_shadingloss(geo, dataset, allrays, allrgbs, tensorf, args, renderer, white_bg, ray_type, device,
                     num_top_rays=1000):
    if num_top_rays == 0:
        depth_xyz = den_eval(geo, dataset, tensorf, args, renderer, N_samples=-1,
          white_bg=white_bg, ray_type=ray_type, device=device, den_thresh=0.7)
    else:
        PSNRs, depth_xyz = ray_evaluation(dataset, allrays, allrgbs, tensorf, args, renderer, N_samples=-1, white_bg=white_bg, ray_type=ray_type, device=device, worse_thresh=0.7)
    smallest_PSNRs, smallest_inds = torch.topk(PSNRs, num_top_rays, dim=0, largest=False, sorted=False)
    top_xyz = depth_xyz[smallest_inds, :]
    print("top_xyz, smallest_PSNRs, smallest_inds", top_xyz.shape, smallest_PSNRs.shape, smallest_inds.shape)
    np.savetxt("log/ship_adapt_full_0.4_0.2_0.1_2222/depth_xyz_top.txt", top_xyz.cpu().numpy(), delimiter=";")
    

def vis_box(geo, args):
    for l in range(len(geo)):
        draw_box(geo[l][..., :3], args.local_range[l], f'{args.basedir}/{args.expname}', l)



if __name__ == '__main__':

    torch.set_default_dtype(torch.float32)
    torch.manual_seed(20211202)
    np.random.seed(20211202)
    args = comp_revise(args) # change some config array to nested array, etc.
    dataset = dataset_dict[args.dataset_name]
    test_dataset = dataset(args.datadir, split='test', downsample=args.downsample_train, is_stack=True, args=args)

    if args.render_only and (args.render_test or args.render_path):
        render_test(args, test_dataset) # run test
    elif args.render_only:
        train_dataset = dataset(args.datadir, split='train', downsample=args.downsample_train, is_stack=True,
                                rnd_ray=False, args=args)
        render_test(args, train_dataset)
    else:  # 1
        train_dataset = dataset(args.datadir, split='train', downsample=args.downsample_train, is_stack=False,
                                rnd_ray=False, args=args)

        pnts = get_density_pnts(args, train_dataset) if args.use_geo < 0 else gen_pnts(
            args)  # a quickly generate points by a dvgo
        # coarse
        # np.savetxt(os.path.dirname(args.ckpt), pnt.cpu().numpy(), delimiter=";")

        # cluster_dict = {
        #     "cluster_xyz": [],
        #     "box_length": [],
        #     "pca_axis": [],
        #     "stds": []
        # }

        cluster_dict, pnts = gen_geo(args, pnts=pnts)  if args.use_geo != 0 else [None, None, None]  # generate tensoRFs' position (xyz)

        ###########
        ##np.savetxt(args.pointfile[:-4] + "_{}_{}_vox_pnts".format(args.datadir.split("/")[-1], args.vox_range[0][0]) + ".txt", pnts.cpu().numpy(), delimiter=";")
        ##vis_box_pca(geo, pca_cluster, args)
        ##print(f"cluster_num={len(geo[0])}")

        reconstruction(args, cluster_dict, train_dataset, test_dataset, pnts)  # run train and test in the end
    if args.export_mesh:
        export_mesh(args, cluster_dict)

 # Test:
 # python train_adapt.py --config configs/adapt_ship/ship_adapt_0.4_0.2_try.txt --ckpt log/ship_adapt_0.4_0.2_try/ship_adapt_0.4_0.2_try.th --info_ckpt log/ship_adapt_0.4_0.2_try/ship_adapt_0.4_0.2_try_meta.pkl --render_only 1 --render_test 0 --render_train 1

