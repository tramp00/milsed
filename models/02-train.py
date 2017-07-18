#!/usr/bin/env python
'''DCASE model skeleton'''

import argparse
import os
import sys
from glob import glob
import six
import pickle
import json

import pandas as pd
import keras as K

from sklearn.model_selection import ShuffleSplit

import pescador
import librosa
import milsed.utils
from jams.util import smkdirs

OUTPUT_PATH = 'resources'


def process_arguments(args):
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument('--alpha', dest='alpha', type=float,
                        default=1.0, help='Alpha parameter for softmax')

    parser.add_argument('--max_samples', dest='max_samples', type=int,
                        default=128,
                        help='Maximum number of samples to draw per streamer')

    parser.add_argument('--patch-duration', dest='duration', type=float,
                        default=10.0,
                        help='Duration (in seconds) of training patches')

    parser.add_argument('--seed', dest='seed', type=int,
                        default='20170612',
                        help='Seed for the random number generator')

    parser.add_argument('--train-streamers', dest='train_streamers', type=int,
                        default=1024,
                        help='Number of active training streamers')

    parser.add_argument('--batch-size', dest='batch_size', type=int,
                        default=32,
                        help='Size of training batches')

    parser.add_argument('--rate', dest='rate', type=int,
                        default=8,
                        help='Rate of pescador stream deactivation')

    parser.add_argument('--epochs', dest='epochs', type=int,
                        default=100,
                        help='Maximum number of epochs to train for')

    parser.add_argument('--epoch-size', dest='epoch_size', type=int,
                        default=512,
                        help='Number of batches per epoch')

    parser.add_argument('--validation-size', dest='validation_size', type=int,
                        default=1024,
                        help='Number of batches per validation')

    parser.add_argument('--early-stopping', dest='early_stopping', type=int,
                        default=20,
                        help='# epochs without improvement to stop')

    parser.add_argument('--reduce-lr', dest='reduce_lr', type=int,
                        default=10,
                        help='# epochs before reducing learning rate')

    parser.add_argument(dest='working', type=str,
                        help='Path to working directory')

    return parser.parse_args(args)


def make_sampler(max_samples, duration, pump, seed):

    n_frames = librosa.time_to_frames(duration,
                                      sr=pump['mel'].sr,
                                      hop_length=pump['mel'].hop_length)[0]

    return pump.sampler(max_samples, n_frames, random_state=seed)


def data_sampler(fname, sampler):
    '''Generate samples from a specified h5 file'''
    for datum in sampler(milsed.utils.load_h5(fname)):
        yield datum


def data_generator(working, tracks, sampler, k, augment=True, batch_size=32,
                   **kwargs):
    '''Generate a data stream from a collection of tracks and a sampler'''

    seeds = []

    for track in tracks:
        fname = os.path.join(working,
                             os.path.extsep.join([str(track), 'h5']))
        seeds.append(pescador.Streamer(data_sampler, fname, sampler))

        if augment:
            for fname in sorted(glob(os.path.join(working,
                                                  '{}.*.h5'.format(track)))):
                seeds.append(pescador.Streamer(data_sampler, fname, sampler))

    # Send it all to a mux
    mux = pescador.Mux(seeds, k, **kwargs)

    if batch_size == 1:
        return mux
    else:
        return pescador.BufferedStreamer(mux, batch_size)


def keras_tuples(gen, inputs=None, outputs=None):

    if isinstance(inputs, six.string_types):
        if isinstance(outputs, six.string_types):
            # One input, one output
            for datum in gen:
                yield (datum[inputs], datum[outputs])
        else:
            # One input, multi outputs
            for datum in gen:
                yield (datum[inputs], [datum[o] for o in outputs])
    else:
        if isinstance(outputs, six.string_types):
            for datum in gen:
                yield ([datum[i] for i in inputs], datum[outputs])
        else:
            # One input, multi outputs
            for datum in gen:
                yield ([datum[i] for i in inputs],
                       [datum[o] for o in outputs])


def construct_model(pump, alpha):

    model_inputs = ['mel/mag']

    # Build the input layer
    layers = pump.layers()

    x_mag = layers['mel/mag']

    # Apply batch normalization
    x_bn = K.layers.BatchNormalization()(x_mag)

    x_sq = milsed.layers.SqueezeLayer()(x_bn)

    # First convolutional filter: a single 3-frame filters
    conv1 = K.layers.Convolution1D(64, 3,
                                   padding='same',
                                   activation='relu',
                                   data_format='channels_last')(x_sq)

    # First recurrent layer: a 128-dim bidirectional gru
    rnn1 = K.layers.Bidirectional(K.layers.GRU(128,
                                               return_sequences=True))(conv1)

    n_classes = pump.fields['static/tags'].shape[0]

    p0 = K.layers.Dense(n_classes, activation='sigmoid')

    p_dynamic = K.layers.TimeDistributed(p0, name='dynamic/tags')(rnn1)

    p_static = milsed.layers.SoftMaxPool(alpha=alpha,
                                         axis=1,
                                         name='static/tags')(p_dynamic)

    model = K.models.Model([x_mag],
                           [p_dynamic, p_static])

    model_outputs = ['dynamic/tags', 'static/tags']

    return model, model_inputs, model_outputs


def train(working, alpha, max_samples, duration, rate,
          batch_size, epochs, epoch_size, validation_size,
          early_stopping, reduce_lr, seed, version):
    '''
    Parameters
    ----------
    working : str
        directory that contains the experiment data (h5)

    alpha : float > 0
        Alpha parameter for softmax

    max_samples : int
        Maximum number of samples per streamer

    duration : float
        Duration of training patches

    batch_size : int
        Size of batches

    rate : int
        Poisson rate for pescador

    epochs : int
        Maximum number of epoch

    epoch_size : int
        Number of batches per epoch

    validation_size : int
        Number of validation batches

    early_stopping : int
        Number of epochs before early stopping

    reduce_lr : int
        Number of epochs before reducing learning rate

    seed : int
        Random seed
    '''

    # Load the pump
    with open(os.path.join(OUTPUT_PATH, 'pump.pkl'), 'rb') as fd:
        pump = pickle.load(fd)

    # Build the sampler
    sampler = make_sampler(max_samples, duration, pump, seed)

    # Build the model
    model, inputs, outputs = construct_model(pump, alpha)

    # Load the training data
    idx_train_ = pd.read_json('index_train.json')

    # Split the training data into train and validation
    splitter_tv = ShuffleSplit(n_splits=1, test_size=0.25,
                               random_state=seed)
    train, val = next(splitter_tv.split(idx_train_))

    idx_train = idx_train_.iloc[train]
    idx_val = idx_train_.iloc[val]

    gen_train = data_generator(working,
                               idx_train['id'].values, sampler, epoch_size,
                               augment=True,
                               lam=rate,
                               batch_size=batch_size,
                               revive=True,
                               random_state=seed)

    gen_train = keras_tuples(gen_train(), inputs=inputs, outputs=outputs)

    gen_val = data_generator(working,
                             idx_val['id'].values, sampler, len(idx_val),
                             augment=False,
                             batch_size=batch_size,
                             revive=True,
                             random_state=seed)

    gen_val = keras_tuples(gen_val(), inputs=inputs, outputs=outputs)

    loss = {'static/tags': 'binary_crossentropy'}

    metrics = {'static/tags': 'accuracy'}

    monitor = 'val_loss'

    model.compile(K.optimizers.Adam(), loss=loss, metrics=metrics)

    # Store the model
    model_spec = K.utils.serialize_keras_object(model)
    with open(os.path.join(OUTPUT_PATH, version, 'model_spec.pkl'),
              'wb') as fd:
        pickle.dump(model_spec, fd)

    # Construct the weight path
    weight_path = os.path.join(OUTPUT_PATH, version, 'model.h5')

    # Build the callbacks
    cb = []
    cb.append(K.callbacks.ModelCheckpoint(weight_path,
                                          save_best_only=True,
                                          verbose=1,
                                          monitor=monitor))

    cb.append(K.callbacks.ReduceLROnPlateau(patience=reduce_lr,
                                            verbose=1,
                                            monitor=monitor))

    cb.append(K.callbacks.EarlyStopping(patience=early_stopping,
                                        verbose=1,
                                        monitor=monitor))

    # Fit the model
    model.fit_generator(gen_train, epoch_size, epochs,
                        validation_data=gen_val,
                        validation_steps=validation_size,
                        callbacks=cb)


if __name__ == '__main__':
    params = process_arguments(sys.argv[1:])

    smkdirs(OUTPUT_PATH)

    version = milsed.utils.increment_version(os.path.join(OUTPUT_PATH,
                                                          'version.txt'))
    smkdirs(os.path.join(OUTPUT_PATH, version))

    print('{}: training'.format(__doc__))
    print('Model version: {}'.format(version))
    print(params)

    # Store the parameters to disk
    with open(os.path.join(OUTPUT_PATH, version, 'params.json'),
              'w') as fd:
        json.dump(vars(params), fd, indent=4)

    train(params.working,
          params.max_samples, params.duration,
          params.rate,
          params.batch_size,
          params.epochs, params.epoch_size,
          params.validation_size,
          params.early_stopping,
          params.reduce_lr,
          params.seed,
          version)