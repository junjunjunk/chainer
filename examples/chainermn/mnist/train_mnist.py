#!/usr/bin/env python
from __future__ import print_function

import argparse

import chainer
import chainer.functions as F
import chainer.links as L
from chainer import training
from chainer.training import extensions
import chainerx

import chainermn


class MLP(chainer.Chain):

    def __init__(self, n_units, n_out):
        super(MLP, self).__init__()
        with self.init_scope():
            # the size of the inputs to each layer will be inferred
            self.l1 = L.Linear(None, n_units)  # n_in -> n_units
            self.l2 = L.Linear(None, n_units)  # n_units -> n_units
            self.l3 = L.Linear(None, n_out)  # n_units -> n_out

    def __call__(self, x):
        h1 = F.relu(self.l1(x))
        h2 = F.relu(self.l2(h1))
        return self.l3(h2)


def main():
    parser = argparse.ArgumentParser(description='ChainerMN example: MNIST')
    parser.add_argument('--batchsize', '-b', type=int, default=100,
                        help='Number of images in each mini-batch')
    parser.add_argument('--communicator', type=str,
                        default='pure_nccl', help='Type of communicator')
    parser.add_argument('--epoch', '-e', type=int, default=20,
                        help='Number of sweeps over the dataset to train')
    parser.add_argument('--gpu', '-g', action='store_true',
                        help='Use GPU')
    parser.add_argument('--chainerx', '-x', action='store_true',
                        default=False, help='Use ChainerX')
    parser.add_argument('--out', '-o', default='result',
                        help='Directory to output the result')
    parser.add_argument('--resume', '-r', default='',
                        help='Resume the training from snapshot')
    parser.add_argument('--unit', '-u', type=int, default=1000,
                        help='Number of units')

    parser.add_argument('--benchmark', action='store_true',
                        help='benchmark mode')
    
    parser.add_argument('--benchmark-iteration', type=int, default=500,
                        help='the number of iterations when using benchmark mode')

    args = parser.parse_args()

    # Prepare ChainerMN communicator.

    if args.gpu:
        if args.communicator == 'naive':
            print('Error: \'naive\' communicator does not support GPU.\n')
            exit(-1)
        comm = chainermn.create_communicator(args.communicator)
        if args.chainerx:
            device = chainer.get_device('cuda:{}'.format(comm.intra_rank))
        else:
            device = chainer.get_device(comm.intra_rank)
    else:
        if args.communicator != 'naive':
            print('Warning: using naive communicator '
                  'because only naive supports CPU-only execution')
        comm = chainermn.create_communicator('naive')
        if args.chainerx:
            device = chainer.get_device('native')
        else:
            device = chainer.get_device(-1)

    if comm.rank == 0:
        print('==========================================')
        print('Num process (COMM_WORLD): {}'.format(comm.size))
        if args.gpu:
            print('Using GPUs')
        print('Using {} communicator'.format(args.communicator))
        print('Num unit: {}'.format(args.unit))
        print('Num Minibatch-size: {}'.format(args.batchsize))
        print('Num epoch: {}'.format(args.epoch))
        print('==========================================')

    model = L.Classifier(MLP(args.unit, 10))

    model.to_device(device)

    # Create a multi node optimizer from a standard Chainer optimizer.
    optimizer = chainermn.create_multi_node_optimizer(
        chainer.optimizers.Adam(), comm)
    optimizer.setup(model)

    # Split and distribute the dataset. Only worker 0 loads the whole dataset.
    # Datasets of worker 0 are evenly split and distributed to all workers.
    if comm.rank == 0:
        train, test = chainer.datasets.get_mnist()
    else:
        train, test = None, None
    train = chainermn.scatter_dataset(train, comm, shuffle=True)
    test = chainermn.scatter_dataset(test, comm, shuffle=True)

    train_iter = chainer.iterators.SerialIterator(train, args.batchsize)
    test_iter = chainer.iterators.SerialIterator(test, args.batchsize,
                                                 repeat=False, shuffle=False)

    if args.benchmark:
        stop_trigger = (args.benchmark_iteration, 'iteration')
    else:
        stop_trigger = (args.epoch, 'epoch')

    updater = training.StandardUpdater(train_iter, optimizer, device=device)
    trainer = training.Trainer(updater, stop_trigger, out=args.out)

    # Create a multi node evaluator from a standard Chainer evaluator.
    evaluator = extensions.Evaluator(test_iter, model, device=device)
    evaluator = chainermn.create_multi_node_evaluator(evaluator, comm)
    trainer.extend(evaluator)

    # Some display and output extensions are necessary only for one worker.
    # (Otherwise, there would just be repeated outputs.)
    if comm.rank == 0:
        if device.xp is not chainerx:
            # Disabled for ChainerX.
            # This is because ChainerX doesn't have a public API set
            # to traverse computational graphs.
            # See examples/mnist/train_mnist.py
            trainer.extend(extensions.DumpGraph('main/loss'))
        trainer.extend(extensions.LogReport())
        trainer.extend(extensions.PrintReport(
            ['epoch', 'main/loss', 'validation/main/loss',
             'main/accuracy', 'validation/main/accuracy', 'elapsed_time']))
        trainer.extend(extensions.ProgressBar())

    if args.resume:
        chainer.serializers.load_npz(args.resume, trainer)

    trainer.run()


if __name__ == '__main__':
    main()
