import lib.toy_data as toy_data
from models import *
import torch
from timeit import default_timer as timer
import lib.utils as utils
import os
import matplotlib.pyplot as plt
import networkx as nx
import math
import seaborn as sns
import UCIdatasets
from lib.visualize_flow import *
sns.set()
flatui = ["#9b59b6", "#3498db", "#95a5a6", "#e74c3c", "#34495e", "#2ecc71"]
sns.palplot(sns.color_palette(flatui))

cond_types = {"DAG": DAGConditioner, "Coupling": CouplingConditioner, "Autoregressive": AutoregressiveConditioner}
norm_types = {"Affine": AffineNormalizer, "Monotonic": MonotonicNormalizer}


def batch_iter(X, batch_size, shuffle=False):
    """
    X: feature tensor (shape: num_instances x num_features)
    """
    if shuffle:
        idxs = torch.randperm(X.shape[0])
    else:
        idxs = torch.arange(X.shape[0])
    if X.is_cuda:
        idxs = idxs.cuda()
    for batch_idxs in idxs.split(batch_size):
        yield X[batch_idxs]


def get_shd(A, B):
    return torch.abs(A - B).sum(), torch.abs(A - B.T).sum(), torch.abs(A + A.T - B - B.T).sum()

def getDataset(ds_name="8-MIX", device="cpu"):
    if ds_name in ["8-MIX", "7-MIX", "3-MIX_DEP", "woodStructural"]:
        x_train = torch.tensor(toy_data.inf_train_gen(ds_name, batch_size=50000)).to(device)
        x_mu = x_train.mean(0)
        x_std = x_train.std(0)
        x_train = (x_train - x_mu.unsqueeze(0).expand(x_train.shape[0], -1)) / x_std.unsqueeze(0).expand(x_train.shape[0],
                                                                                                      -1)

        x_test = torch.tensor(toy_data.inf_train_gen(ds_name, batch_size=5000)).to(device)
        x_test = (x_test - x_mu.unsqueeze(0).expand(x_test.shape[0], -1)) / x_std.unsqueeze(0).expand(x_test.shape[0],
                                                                                                      -1)

        x_valid = torch.tensor(toy_data.inf_train_gen(ds_name, batch_size=5000)).to(device)
        x_valid = (x_valid - x_mu.unsqueeze(0).expand(x_test.shape[0], -1)) / x_std.unsqueeze(0).expand(x_valid.shape[0],
                                                                                                      -1)

        ground_truth_A = torch.tensor(toy_data.getA(ds_name))
    elif ds_name == "proteins":
        data = UCIdatasets.PROTEINS()
        x_train = torch.from_numpy(data.trn.x).to(device)
        x_test = torch.from_numpy(data.val.x).to(device)
        x_valid = torch.from_numpy(data.tst.x).to(device)
        ground_truth_A = torch.tensor(UCIdatasets.proteins.get_adj_matrix())
    else:
        return None

    return (x_train, x_test, x_valid), ground_truth_A








def train_toy(toy, load=True, nb_step_dual=300, nb_steps=15, folder="", l1=1., nb_epoch=20000, pre_heating_epochs=10,
              nb_flow=1, cond_type = "DAG", emb_net = [150, 150, 150, 30], norm_type="Affine", use_A=False):
    logger = utils.get_logger(logpath=os.path.join(folder, toy, 'logs'), filepath=os.path.abspath(__file__))

    logger.info("Creating model...")

    device = "cpu" if not(torch.cuda.is_available()) else "cuda:0"

    (x_train, x_test, x_valid), ground_truth_A = getDataset(toy, "cpu")
    ground_truth_A = ground_truth_A.to(device)
    params = {'batch_size': 100,
              'shuffle': True,
              'num_workers': 0,
              'pin_memory': False}
    train_generator = torch.utils.data.DataLoader(x_train.to(device), **params)
    valid_generator = torch.utils.data.DataLoader(x_valid.to(device), **params)
    dim = x_train.shape[1]

    norm_type = norm_type
    save_name = norm_type + str(emb_net) + str(nb_flow) + str(use_A)
    solver = "CCParallel"
    int_net = [50, 50, 50]

    conditioner_type = cond_types[cond_type]
    conditioner_args = {"in_size": dim, "hidden": emb_net[:-1], "out_size": emb_net[-1]}
    if conditioner_type is DAGConditioner:
        conditioner_args['l1'] = l1
        conditioner_args['gumble_T'] = .5
        conditioner_args['nb_epoch_update'] = nb_step_dual
        conditioner_args["hot_encoding"] = True
    normalizer_type = norm_types[norm_type]
    if normalizer_type is MonotonicNormalizer:
        normalizer_args = {"integrand_net": int_net, "cond_size": emb_net[-1], "nb_steps": nb_steps,
                           "solver": solver}
    else:
        normalizer_args = {}

    model = buildFCNormalizingFlow(nb_flow, conditioner_type, conditioner_args, normalizer_type, normalizer_args).to(device)

    if use_A:
        with torch.no_grad():
            cond = model.getConditioners()[0]
            cond.A.copy_(ground_truth_A)
            cond.post_process()

    opt = torch.optim.Adam(model.parameters(), 1e-3, weight_decay=1e-5)

    if load:
        logger.info("Loading model...")
        model.load_state_dict(torch.load(folder + toy + '/' + save_name + 'model.pt'))
        model.train()
        opt.load_state_dict(torch.load(folder + toy + '/' + save_name + 'ADAM.pt'))
        logger.info("Model loaded.")

    if True:
        for step in model.steps:
            step.conditioner.stoch_gate = True
            step.conditioner.noise_gate = False
            step.conditioner.gumble_T = .5
    torch.autograd.set_detect_anomaly(True)
    for epoch in range(nb_epoch):
        loss_tot = 0
        start = timer()
        for j, cur_x in enumerate(train_generator):
            z, jac = model(cur_x)
            loss = model.loss(z, jac)
            loss_tot += loss.detach()
            if math.isnan(loss.item()):
                ll, z = model.compute_ll(cur_x)
                print(ll)
                print(z)
                print(ll.max(), z.max())
                exit()
            opt.zero_grad()
            loss.backward(retain_graph=True)
            opt.step()
        loss_tot /= j
        model.step(epoch, loss_tot)

        end = timer()
        loss_tot_valid = 0
        with torch.no_grad():
            for j, cur_x in enumerate(valid_generator):
                z, jac = model(cur_x.to(device))
                loss = model.loss(z, jac)
                loss_tot_valid += loss.detach()
        loss_tot_valid /= j
        dagness = max(model.DAGness())
        if model.isInvertible():
            A = model.getConditioners()[0].A
            shd, shd_inv, shd_bis = get_shd(A, ground_truth_A)
            TP = (A * (1 - torch.abs(A - ground_truth_A))).sum()
            rev_TP = (A.T * (1 - torch.abs(A.T - ground_truth_A))).sum()
            logger.info("SHD: {:} - {:} - {:} - TP: {:} - reversed: {:}".format(shd, shd_inv, shd_bis, TP, rev_TP))
        logger.info("epoch: {:d} - Train loss: {:4f} - Valid loss: {:4f} - <<DAGness>>: {:4f} - Elapsed time per epoch {:4f} (seconds)".
                    format(epoch, loss_tot.item(), loss_tot_valid.item(), dagness, end-start))

        if epoch % 50 == 0:
                with torch.no_grad():
                    fig, axes = plt.subplots(nrows=1, ncols=2)
                    pos = axes[0].matshow(model.getConditioners()[0].A.detach().cpu().numpy())
                    fig.colorbar(pos, ax=axes[0])
                    axes[1].matshow(ground_truth_A.detach().cpu().numpy())
                    plt.savefig("%s%s/flow_%s_%d.pdf" % (folder, toy, save_name, epoch))
                    torch.save(model.state_dict(), folder + toy + '/' + save_name + 'model.pt')
                    torch.save(opt.state_dict(), folder + toy + '/'+ save_name + 'ADAM.pt')
                    if toy == "3-MIX_DEP":
                        fig, axes = plt.subplots(nrows=3, ncols=3, figsize=(18, 18))

                        def plot_sample_2D(i):
                            def sample(z):
                                z_all = torch.zeros(z.shape[0], 6).to(device)
                                z_all[:, [2*i, 2*i + 1]] = z.to(device)
                                return model.invert(z_all)[:, [2*i, 2*i + 1]].cpu()
                            return sample

                        def plot_dens_2D(i):
                            def sample(x):
                                n_pts = 3
                                p_x = 0.
                                for j in range(500):
                                    x_all = x_valid[j*n_pts:(j + 1)*n_pts].unsqueeze(0).expand(x.shape[0], -1, -1).clone().to(device)
                                    x_all[:, :,[2*i, 2*i + 1]] = x.unsqueeze(1).expand(-1, n_pts, -1).to(device)
                                    z, jac = model(x_all.view(-1, 6))
                                    p_x += torch.exp((jac + model.z_log_density(z)).view(x.shape[0], n_pts, -1)).sum(1)
                                return p_x.cpu(), None
                            return sample

                        for i in range(3):
                            plt_flow_samples(torch.randn, plot_sample_2D(i), axes[i, 0], 100)
                            plt_samples(x_test[:, [2*i, 2*i + 1]].clone().detach().cpu().numpy(),
                                        axes[i, 1])
                            plt_flow(plot_dens_2D(i), axes[i, 2], npts=100)
                        plt.savefig("%s%s/flow_%s_%d_vizu.pdf" % (folder, toy, save_name, epoch))

toy = "8gaussians"

import argparse
datasets = ["8-MIX", "7-MIX", "woodStructural", "proteins", "3-MIX_DEP"]

parser = argparse.ArgumentParser(description='')
parser.add_argument("-dataset", default=None, choices=datasets, help="Which toy problem ?")
parser.add_argument("-load", default=False, action="store_true", help="Load a model ?")
parser.add_argument("-folder", default="", help="Folder")
parser.add_argument("-nb_steps_dual", default=50, type=int, help="number of step between updating Acyclicity constraint and sparsity constraint")
parser.add_argument("-l1", default=.0, type=float, help="Maximum weight for l1 regularization")
parser.add_argument("-nb_epoch", default=20000, type=int, help="Number of epochs")
parser.add_argument("-norm_type", default="Affine")
parser.add_argument("-use_A", default=False, action="store_true")
parser.add_argument("-emb_net", default=[100, 100, 100, 10], nargs="+", type=int, help="NN layers of embedding")

args = parser.parse_args()


if args.dataset is None:
    toys = datasets
else:
    toys = [args.dataset]

for toy in toys:
    if not(os.path.isdir(args.folder + toy)):
        os.makedirs(args.folder + toy)
    train_toy(toy, load=args.load, folder=args.folder, nb_step_dual=args.nb_steps_dual, l1=args.l1,
              nb_epoch=args.nb_epoch, norm_type=args.norm_type, use_A=args.use_A, emb_net=args.emb_net)
