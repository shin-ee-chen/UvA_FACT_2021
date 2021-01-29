import os
import argparse

import torch
import torch.utils.data as data

from models.mnist_cnn import MNIST_CNN
from datasets.mnist import MNIST_limited

from mnist_cvae_train import GenerateCallback
from models.cvae import MNIST_CVAE
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

import matplotlib.pyplot as plt

import numpy as np
import torch.nn.functional as F

def train(args):
    """
    Inputs:
        args - Namespace object from the argparser
    """
    print("Hi let us start to the information flow and accuracies for ablation study!")
    M = len(args.classes)

    # load classifier
    classifier = MNIST_CNN(model_param_set=args.clf_param_set, M=M,
                        lr=args.lr, momentum=args.momentum)

    classifier_path = './pretrained_models/mnist_cnn_149/'
    checkpoint_model = torch.load(os.path.join(classifier_path,'model.pt'), map_location=device)
    classifier.load_state_dict(checkpoint_model['model_state_dict_classifier'])

    # load GCE
    gce_path = './pretrained_models/mnist_gce_149/'
    gce = torch.load(os.path.join(gce_path,'gce_model.pt'), map_location=device)

    print("pretrained model loaded!")

    # plot information_flow
    z_dim = args.K + args.L
    info_flow = gce.information_flow_single(range(0,z_dim))

    # we use author's code for making the exact same plot
    cols = {'golden_poppy' : [1.000,0.761,0.039],
        'bright_navy_blue' : [0.047,0.482,0.863],
        'rosso_corsa' : [0.816,0.000,0.000]}
    x_labels = ('$\\alpha_1$', '$\\alpha_2$', '$\\beta_1$', '$\\beta_2$')
    fig, ax = plt.subplots()
    ax.bar(range(z_dim), info_flow, color=[
        cols['rosso_corsa'], cols['rosso_corsa'], cols['bright_navy_blue'],
        cols['bright_navy_blue']])
    plt.xticks(range(z_dim), x_labels)
    ax.yaxis.grid(linewidth='0.3')
    plt.ylabel('Information flow to $\\widehat{Y}$')
    plt.title('Information flow of individual causal factors')
    plt.savefig('./figures/ablation_study/information_flow.svg')
    plt.savefig('./figures/ablation_study/information_flow.png')
    print("done 5a")

    # --- load test data ---
    train_set, valid_set = MNIST_limited(train=True, classes=args.classes)

    valid_loader = data.DataLoader(valid_set, batch_size=1, shuffle=False,
                                   drop_last=True, pin_memory=True, num_workers=0)
    X = train_set.data
    Y = train_set.targets
    vaX = valid_set.data
    vaY = valid_set.targets

    ntrain, nrow, ncol = X.shape
    x_dim = nrow*ncol

    # --- compute classifier accuracy after 'removing' latent factors ---
    classifier_accuracy_original = np.zeros(z_dim)
    Yhat = np.zeros((len(vaX)))
    Yhat_reencoded = np.zeros((len(vaX)))
    Yhat_aspectremoved = np.zeros((z_dim, len(vaX)))

    for i_samp in range(len(vaX)):
        if (i_samp % 1000) == 0:
            print(i_samp)
        dataloader_iterator = iter(valid_loader)
        vaX1, vaY1 = next(dataloader_iterator)
        x = torch.from_numpy(np.asarray(vaX[None, i_samp:i_samp+1,:,:])).float().to(device)

        Yhat[i_samp] = np.argmax(F.softmax(classifier(x.cpu()), dim=1).cpu().detach().numpy())
        z = gce.encoder(x.to(device))[0]
        xhat = gce.decoder(z)
        xhat = torch.sigmoid(xhat)
        Yhat_reencoded[i_samp] = np.argmax(F.softmax(classifier(xhat.cpu()), dim=1).cpu().detach().numpy())
        for i_latent in range(z_dim):
            z = gce.encoder(x.to(device))[0]
            z[0,i_latent] = torch.randn((1))
            xhat = gce.decoder(z)
            xhat = torch.sigmoid(xhat)
            Yhat_aspectremoved[i_latent,i_samp] = np.argmax(F.softmax(classifier(xhat.cpu()), dim=1).cpu().detach().numpy())
    vaY = np.asarray(vaY)
    Yhat = np.asarray(Yhat)
    Yhat_reencoded = np.asarray(Yhat_reencoded)

    classifier_accuracy = np.mean(vaY == Yhat)
    classifier_accuracy_reencoded = np.mean(vaY == Yhat_reencoded)
    classifier_accuracy_aspectremoved = np.zeros((z_dim))
    for i in range(z_dim):
        classifier_accuracy_aspectremoved[i] = np.mean(vaY == Yhat_aspectremoved[i,:])

    print(classifier_accuracy, classifier_accuracy_reencoded, classifier_accuracy_aspectremoved)

    # --- plot classifier accuracy ---
    # we use author's code for making the exact same plot
    cols = {'black' : [0.000, 0.000, 0.000],
            'golden_poppy' : [1.000,0.761,0.039],
            'bright_navy_blue' : [0.047,0.482,0.863],
            'rosso_corsa' : [0.816,0.000,0.000]}
    x_labels = ('orig','reenc','$\\alpha_1$', '$\\alpha_2$', '$\\beta_1$', '$\\beta_2$')
    fig, ax = plt.subplots()
    ax.yaxis.grid(linewidth='0.3')
    ax.bar(range(z_dim+2), np.concatenate(([classifier_accuracy],
                                           [classifier_accuracy_reencoded],
                                           classifier_accuracy_aspectremoved)),
           color=[cols['black'], cols['black'], cols['rosso_corsa'],
                  cols['rosso_corsa'], cols['bright_navy_blue'], cols['bright_navy_blue']])
    plt.xticks(range(z_dim+2), x_labels)
    plt.ylim((0.2,1.0))
    plt.yticks((0.2,0.4,0.6))
    plt.ylabel('Classifier accuracy')
    plt.title('Classifier accuracy after removing aspect')
    plt.savefig('./figures/ablation_study/accuracy_comparison.svg')
    plt.savefig('./figures/ablation_study/accuracy_comparison.png')





if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    # Model hyperparameters
    parser.add_argument('--clf_param_set', default='OShaugnessy',
                        type=str, help='The black-box classifier we wish to explain.')
    parser.add_argument('--classes', default=[1, 4, 9],
                        type=int, nargs='+',
                        help='The classes permittible for classification')
    # Loss and optimizer hyperparameters
    parser.add_argument('--lr', default=5e-4, type=float,
                        help='Learning rate to use')
    parser.add_argument('--momentum', default=0.9, type=float,
                        help='Learning rate to use')

    # Debug parameters
    parser.add_argument('--debug_version', default=False,
                        help=('Whether to check debugs, etc.'))
    parser.add_argument('--fast_dev_run', default=False,
                        help=('Whether to check debugs, etc.'))
    parser.add_argument('--gpu', default=True, action='store_true',
                        help=('Whether to train on GPU (if available) or CPU'))

    # param for cvae
    parser.add_argument('--K', default=2, type=int,
                       help='Dimensionality of causal latent space')
    parser.add_argument('--L', default=2, type=int,
                       help='Dimensionality of non-causal latent space')
    parser.add_argument('--M', default=3, type=int,
                       help='Dimensionality of classifier output')

    args = parser.parse_args()

    train(args)