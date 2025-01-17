import os
import argparse
from distutils.util import strtobool

import torch
import pytorch_lightning as pl
from pytorch_lightning.callbacks import ModelCheckpoint
import torch.utils.data as data

from models.mnist_cnn import MNIST_CNN
from utils.reproducibility import set_seed, set_deterministic
from datasets.mnist import MNIST_limited
from datasets.fashion_mnist import Fashion_MNIST_limited
from utils.timing import Timer

CHECKPOINT_PATH =  './checkpoints'

def train(args):
    """
    Trains the classifier.
    Inputs:
        args - Namespace object from the argparser defining the hyperparameters etc.
    """
    
    if args.add_classes_to_cpt_path:
        classes_str = ''.join(str(x) for x in sorted(args.classes))
        args.log_dir +=  '_' + classes_str
    full_log_dir = os.path.join(CHECKPOINT_PATH, args.log_dir)
    os.makedirs(full_log_dir, exist_ok=True)
    os.makedirs(os.path.join(full_log_dir, "lightning_logs"), exist_ok=True) # to fix "Missing logger folder"

    M = len(args.classes)

    if args.datasets == 'traditional':
        train_set, valid_set = MNIST_limited(train=True, classes=args.classes)
        test_set = MNIST_limited(train=False, classes=args.classes)
    else:
        train_set, valid_set = Fashion_MNIST_limited(train=True, classes=args.classes)
        test_set = Fashion_MNIST_limited(train=False, classes=args.classes)

    train_loader = data.DataLoader(train_set, batch_size=args.batch_size, shuffle=True,
                                   drop_last=True, pin_memory=True, num_workers=args.num_workers)
    valid_loader = data.DataLoader(valid_set, batch_size=args.batch_size, shuffle=False,
                                   drop_last=True, pin_memory=True, num_workers=args.num_workers)
    test_loader = data.DataLoader(test_set, batch_size=args.batch_size, shuffle=False,
                                  drop_last=True, pin_memory=True, num_workers=args.num_workers)

    # Create a PyTorch Lightning trainer with the generation callback
    trainer = pl.Trainer(default_root_dir=full_log_dir,
                         checkpoint_callback=ModelCheckpoint(
                             save_weights_only=True, mode="max", monitor="Valid acc"),
                         gpus=1 if (torch.cuda.is_available() and
                                    args.gpu) else 0,
                         max_epochs=args.max_epochs,
                         progress_bar_refresh_rate=args.progress_bar)

    trainer.logger._default_hp_metric = None

    set_seed(42)
    set_deterministic()

    model = MNIST_CNN(model_param_set=args.clf_param_set, M=M,
                        lr=args.lr, momentum=args.momentum)

    timer = Timer()
    trainer.fit(model, train_loader, valid_loader)
    print(f"Total training time: {timer.time()}")

    # Eval post training
    model = MNIST_CNN.load_from_checkpoint(
        trainer.checkpoint_callback.best_model_path)

    # Test results
    val_result = trainer.test(
        model, test_dataloaders=valid_loader, verbose=False)
    test_result = trainer.test(
        model, test_dataloaders=test_loader, verbose=False)
    result = {"Test": test_result[0]["Test acc"],
              "Valid": val_result[0]["Test acc"]}

    save_folder = os.path.join("pretrained_models", args.log_dir)
    os.makedirs(save_folder, exist_ok=True)
    torch.save({
    'model_state_dict_classifier': model.state_dict()
        }, os.path.join(save_folder, 'model.pt'))

    return model, result

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    # Model hyperparameters
    parser.add_argument('--clf_param_set', default='OShaugnessy',
                        type=str, help='The black-box classifier we wish to explain.')

    parser.add_argument('--classes', default=[3, 8],
                        type=int, nargs='+',
                        help='The classes permittible for classification')

    # Loss and optimizer hyperparameters
    parser.add_argument('--lr', default=0.1, type=float,
                        help='Learning rate to use')
    parser.add_argument('--momentum', default=0.5, type=float,
                        help='Learning rate to use')
    parser.add_argument('--batch_size', default=64, type=int,
                        help='Minibatch size')
    parser.add_argument('--max_epochs', default=20, type=int,
                        help='Max number of training epochs')

    # Other hyperparameters

    parser.add_argument('--seed', default=42, type=int,
                        help='Seed to use for reproducing results')
    parser.add_argument('--num_workers', default=0, type=int,
                        help='Number of workers to use in the data loaders.')
    parser.add_argument('--progress_bar', default=True, type=lambda x: bool(strtobool(x)),
                        help=('Use a progress bar indicator for interactive experimentation. '
                              'Not to be used in conjuction with SLURM jobs'))
    parser.add_argument('--log_dir', default='mnist_cnn', type=str,
                        help='Name of the subdirectory for PyTorch Lightning logs and the final model. \
                              Automatically adds the classes to directory. \
                              If this is not needed, turn off using add_classes_to_cpt_path flag.')
    parser.add_argument('--add_classes_to_cpt_path', default=True, type=lambda x: bool(strtobool(x)),
                        help='Whether to add the classes to log_dir.')
                        
    parser.add_argument('--datasets', default='traditional',choices=['traditional', 'fashion'],
                        help='Datasets used for training: traditional or fashion')

    # Debug parameters
    parser.add_argument('--debug_version', default=False, type=lambda x: bool(strtobool(x)),
                        help=('Whether to check debugs, etc.'))
    parser.add_argument('--fast_dev_run', default=False, type=lambda x: bool(strtobool(x)),
                        help=('Whether to check debugs, etc.'))
    parser.add_argument('--gpu', default=True, type=lambda x: bool(strtobool(x)),
                        help=('Whether to train on GPU (if available) or CPU'))

    args = parser.parse_args()

    model, results = train(args)

    print(results)
