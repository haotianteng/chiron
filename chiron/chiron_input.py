# Copyright 2017 The Chiron Authors. All Rights Reserved.
#
#This Source Code Form is subject to the terms of the Mozilla Public
#License, v. 2.0. If a copy of the MPL was not distributed with this
#file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
#Created on Mon Mar 27 14:04:57 2017

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import collections
import os
import sys
import tempfile

import h5py
import numpy as np
from statsmodels import robust
from six.moves import range
from six.moves import zip
import tensorflow as tf
from chiron.utils import progress
from chiron import __version__
from packaging import version
SIGNAL_DTYPE=np.int16
raw_labels = collections.namedtuple('raw_labels', ['start', 'length', 'base'])
MIN_LABEL_LENGTH = 2
MIN_SIGNAL_PRO = 0.3
MEDIAN=0
MEAN=1
class Flags(object):
    def __init__(self):
        self.max_segments_number = None
        self.MAXLEN = 1e5  # Maximum Length of the holder in biglist. 1e5 by default
        self.sig_norm = None

#        self.max_segment_len = 200
FLAGS = Flags()


class biglist(object):
    """
    biglist class, read into memory if reads number < MAXLEN, otherwise read into a hdf5 file.
    """

    def __init__(self, 
                 data_handle, 
				 dtype='float32', 
				 length=0, 
				 cache=False,
                 max_len=1e5):
        self.handle = data_handle
        self.dtype = dtype
        self.holder = list()
        self.length = length
        self.max_len = max_len
        self.cache = cache  # Mark if the list has been saved into hdf5 or not

    @property
    def shape(self):
        return self.handle.shape

    def append(self, item):
        self.holder.append(item)
        self.check_save()

    def __add__(self, add_list):
        self.holder += add_list
        self.check_save()
        return self

    def __len__(self):
        return self.length + len(self.holder)

    def resize(self, size, axis=0):
        self.save_rest()
        if self.cache:
            self.handle.resize(size, axis=axis)
            self.length = len(self.handle)
        else:
            self.holder = self.holder[:size]

    def save_rest(self):
        if self.cache:
            if len(self.holder) != 0:
                self.save()

    def check_save(self):
        if len(self.holder) > self.max_len:
            self.save()
            self.cache = True

    def save(self):
        if type(self.holder[0]) is list:
            max_sub_len = max([len(sub_a) for sub_a in self.holder])
            shape = self.handle.shape
            for item in self.holder:
                item.extend([0] * (max(shape[1], max_sub_len) - len(item)))
            if max_sub_len > shape[1]:
                self.handle.resize(max_sub_len, axis=1)
            self.handle.resize(self.length + len(self.holder), axis=0)
            self.handle[self.length:] = self.holder
            self.length += len(self.holder)
            del self.holder[:]
            self.holder = list()
        else:
            self.handle.resize(self.length + len(self.holder), axis=0)
            self.handle[self.length:] = self.holder
            self.length += len(self.holder)
            del self.holder[:]
            self.holder = list()

    def __getitem__(self, val):
        if self.cache:
            if len(self.holder) != 0:
                self.save()
            return self.handle[val]
        else:
            return self.holder[val]


class DataSet(object):
    def __init__(self,
                 event,
                 event_length,
                 label,
                 label_length,
                 for_eval=False,
                 ):
        """Custruct a DataSet."""
        if for_eval == False:
            assert len(event) == len(label) and len(event_length) == len(
                label_length) and len(event) == len(
                event_length), "Sequence length for event \
            and label does not of event and label should be same, \
            event:%d , label:%d" % (len(event), len(label))
        self._event = event
        self._event_length = event_length
        self._label = label
        self._label_length = label_length
        self._reads_n = len(event)
        self._epochs_completed = 0
        self._index_in_epoch = 0
        self._for_eval = for_eval
        self._perm = np.arange(self._reads_n)

    @property
    def event(self):
        return self._event

    @property
    def label(self):
        return self._label

    @property
    def event_length(self):
        return self._event_length

    @property
    def label_length(self):
        return self._label_length

    @property
    def reads_n(self):
        return self._reads_n

    @property
    def index_in_epoch(self):
        return self._index_in_epoch

    @property
    def epochs_completed(self):
        return self._epochs_completed

    @property
    def for_eval(self):
        return self._for_eval

    @property
    def perm(self):
        return self._perm

    def read_into_memory(self, index):
        event = np.asarray(list(zip([self._event[i] for i in index],
                                    [self._event_length[i] for i in index])))
        if not self.for_eval:
            label = np.asarray(list(zip([self._label[i] for i in index],
                                        [self._label_length[i] for i in index])))
        else:
            label = []
        return event, label

    def next_batch(self, batch_size, shuffle=True):
        """Return next batch in batch_size from the data set.
            Input Args:
                batch_size:A scalar indicate the batch size.
                shuffle: boolean, indicate if the data should be shuffled after each epoch.
            Output Args:
                inputX,sequence_length,label_batch: tuple of (indx,vals,shape)
        """
        if self.epochs_completed>=1 and self.for_eval:
            print("Warning, evaluation dataset already finish one iteration.")
        start = self._index_in_epoch
        # Shuffle for the first epoch
        if self._epochs_completed == 0 and start == 0:
            if shuffle:
                np.random.shuffle(self._perm)
        # Go to the next epoch
        if start + batch_size >= self.reads_n:
            # Finished epoch
            self._epochs_completed += 1
            # Get the rest samples in this epoch
            rest_reads_n = self.reads_n - start
            event_rest_part, label_rest_part = self.read_into_memory(
                self._perm[start:self._reads_n])
            start = 0
            if self._for_eval:
                event_batch = event_rest_part
                label_batch = label_rest_part
                self._index_in_epoch = 0
                end = 0
            # Shuffle the data
            else:
                if shuffle:
                    np.random.shuffle(self._perm)
                # Start next epoch
                self._index_in_epoch = batch_size - rest_reads_n
                end = self._index_in_epoch
                event_new_part, label_new_part = self.read_into_memory(
                    self._perm[start:end])
                if event_rest_part.shape[0] == 0:
                    event_batch = event_new_part
                    label_batch = label_new_part
                elif event_new_part.shape[0] == 0:
                    event_batch = event_rest_part
                    label_batch = label_rest_part
                else:
                    event_batch = np.concatenate((event_rest_part, event_new_part), axis=0)
                    label_batch = np.concatenate((label_rest_part, label_new_part), axis=0)
        else:
            self._index_in_epoch += batch_size
            end = self._index_in_epoch
            event_batch, label_batch = self.read_into_memory(
                self._perm[start:end])
        if not self._for_eval:
            label_batch = batch2sparse(label_batch)
        seq_length = event_batch[:, 1].astype(np.int32)
        return np.vstack(event_batch[:, 0]).astype(
            np.float32), seq_length, label_batch


def read_data_for_eval(file_path, 
					   start_index=0,
                       step=20, 
	                   seg_length=200, 
                       reverse_fast5 = False):
    """
    Input Args:
        file_path: file path to a signal/fast5 file.
        start_index: the index of the signal start to read.
        step: sliding step size.
        seg_length: length of segments.
        sig_norm: The way signal being normalized, keep it the same as it during training.
        reverse_fast5: if the signal need to be reversed from a fast5 file.
    """
    if file_path.endswith('.signal'):
        f_signal = read_signal(file_path, normalize=FLAGS.sig_norm)
    elif file_path.endswith('.fast5'):
        f_signal = read_signal_fast5(file_path, normalize=FLAGS.sig_norm)
        if reverse_fast5:
            f_signal = f_signal[::-1]
    else:
        raise TypeError("Input file should be a signal file or fsat5 file, but a %s file is given."%(file_path))
    event = list()
    event_len = list()
    label = list()
    label_len = list()
    f_signal = f_signal[start_index:]
    sig_len = len(f_signal)
    for indx in range(0, sig_len, step):
        segment_sig = f_signal[indx:indx + seg_length]
        segment_len = len(segment_sig)
        padding(segment_sig, seg_length)
        event.append(segment_sig)
        event_len.append(segment_len)
    evaluation = DataSet(event=event,
                         event_length=event_len,
                         label=label,
                         label_length=label_len,
                         for_eval=True)
    return evaluation


def read_cache_dataset(h5py_file_path):
    """Notice: Return a data reader for a h5py_file, call this function multiple
    time for parallel reading, this will give you N dependent dataset reader,
    each reader read independently from the h5py file."""
    hdf5_record = h5py.File(h5py_file_path, "r")
    event_h = hdf5_record['event/record']
    event_length_h = hdf5_record['event/length']
    label_h = hdf5_record['label/record']
    label_length_h = hdf5_record['label/length']
    event_len = len(event_h)
    label_len = len(label_h)
    assert len(event_h) == len(event_length_h)
    assert len(label_h) == len(label_length_h)
    event = biglist(data_handle=event_h, length=event_len, cache=True)
    event_length = biglist(data_handle=event_length_h, length=event_len,
                           cache=True)
    label = biglist(data_handle=label_h, length=label_len, cache=True)
    label_length = biglist(data_handle=label_length_h, length=label_len,
                           cache=True)
    return DataSet(event=event, event_length=event_length, label=label,
                   label_length=label_length)


def read_tfrecord(data_dir, 
                  tfrecord, 
                  h5py_file_path=None, 
                  seq_length=300, 
                  k_mer=1, 
                  max_segments_num=None,
                  skip_start = 10):
    ###This method deprecated please use read_raw_data_sets instead
    ###Read from raw data
    count_bar = progress.multi_pbars("Extract tfrecords")
    if max_segments_num is None:
        max_segments_num = FLAGS.max_segments_number
        count_bar.update(0,progress = 0,total = max_segments_num)
    if h5py_file_path is None:
        h5py_file_path = tempfile.mkdtemp() + '/temp_record.hdf5'
    else:
        try:
            os.remove(os.path.abspath(h5py_file_path))
        except:
            pass
        if not os.path.isdir(os.path.dirname(os.path.abspath(h5py_file_path))):
            os.mkdir(os.path.dirname(os.path.abspath(h5py_file_path)))
    with h5py.File(h5py_file_path, "a") as hdf5_record:
        event_h = hdf5_record.create_dataset('event/record', dtype='float32', shape=(0, seq_length),
                                             maxshape=(None, seq_length))
        event_length_h = hdf5_record.create_dataset('event/length', dtype='int32', shape=(0,), maxshape=(None,),
                                                    chunks=True)
        label_h = hdf5_record.create_dataset('label/record', dtype='int32', shape=(0, 0), maxshape=(None, seq_length))
        label_length_h = hdf5_record.create_dataset('label/length', dtype='int32', shape=(0,), maxshape=(None,))
        event = biglist(data_handle=event_h, max_len=FLAGS.MAXLEN)
        event_length = biglist(data_handle=event_length_h, max_len=FLAGS.MAXLEN)
        label = biglist(data_handle=label_h, max_len=FLAGS.MAXLEN)
        label_length = biglist(data_handle=label_length_h, max_len=FLAGS.MAXLEN)
        count = 0
        file_count = 0

        tfrecords_filename = data_dir + tfrecord
        record_iterator = tf.python_io.tf_record_iterator(path=tfrecords_filename)

        for string_record in record_iterator:
            
            example = tf.train.Example()
            example.ParseFromString(string_record)
            
            raw_data_string = (example.features.feature['raw_data']
                                          .bytes_list
                                          .value[0])
            features_string = (example.features.feature['features']
                                        .bytes_list
                                        .value[0])
            fn_string = (example.features.feature['fname'].bytes_list.value[0])

            raw_data = np.frombuffer(raw_data_string, dtype=SIGNAL_DTYPE)
            
            features_data = np.frombuffer(features_string, dtype='S8')
            # grouping the whole array into sub-array with size = 3
            group_size = 3
            features_data = [features_data[n:n+group_size] for n in range(0, len(features_data), group_size)]
            f_signal = read_signal_tfrecord(raw_data,normalize = FLAGS.sig_norm)

            if len(f_signal) == 0:
                continue
            #try:
            f_label = read_label_tfrecord(features_data, skip_start=skip_start, window_n=(k_mer - 1) / 2)
            #except:
            #    sys.stdout.write("Read the label fail.Skipped.")
            #    continue
            try:
                tmp_event, tmp_event_length, tmp_label, tmp_label_length = read_raw(f_signal, f_label, seq_length)
            except Exception as e:
                print("Extract label from %s fail, label position exceed max signal length."%(fn_string))
                raise e
            event += tmp_event
            event_length += tmp_event_length
            label += tmp_label
            label_length += tmp_label_length
            del tmp_event
            del tmp_event_length
            del tmp_label
            del tmp_label_length
            count = len(event)
            if file_count % 10 == 0:
                if max_segments_num is not None:
                    count_bar.update(0,progress = count,total = max_segments_num)
                    count_bar.update_bar()
                    if len(event) > max_segments_num:
                        event.resize(max_segments_num)
                        label.resize(max_segments_num)
                        event_length.resize(max_segments_num)

                        label_length.resize(max_segments_num)
                        break
                else:
                    count_bar.update(0,progress = count,total = count)
                    count_bar.update_bar()
            file_count += 1
        if event.cache:
            event.save_rest()
            event_length.save_rest()
            label.save_rest()
            label_length.save_rest()
            train = read_cache_dataset(h5py_file_path)
        else:
            event.save()
            event_length.save()
            label.save()
            label_length.save()
            train = read_cache_dataset(h5py_file_path)
        count_bar.end()
    return train
            
def read_raw_data_sets(data_dir, 
                       h5py_file_path=None, 
                       seq_length=300, 
                       k_mer=1, 
                       max_segments_num=FLAGS.max_segments_number,
                       skip_start = 10):
    ###Read from raw data
    count_bar = progress.multi_pbars("Extract tfrecords")
    if max_segments_num is None:
        max_segments_num = FLAGS.max_segments_number
        count_bar.update(0,progress = 0,total = max_segments_num)
    if h5py_file_path is None:
        h5py_file_path = tempfile.mkdtemp() + '/temp_record.hdf5'
    else:
        try:
            os.remove(os.path.abspath(h5py_file_path))
        except:
            pass
        if not os.path.isdir(os.path.dirname(os.path.abspath(h5py_file_path))):
            os.mkdir(os.path.dirname(os.path.abspath(h5py_file_path)))
    with h5py.File(h5py_file_path, "a") as hdf5_record:
        event_h = hdf5_record.create_dataset('event/record', dtype='float32', shape=(0, seq_length),
                                             maxshape=(None, seq_length))
        event_length_h = hdf5_record.create_dataset('event/length', dtype='int32', shape=(0,), maxshape=(None,),
                                                    chunks=True)
        label_h = hdf5_record.create_dataset('label/record', dtype='int32',
                                             shape=(0, 0),
                                             maxshape=(None, seq_length))
        label_length_h = hdf5_record.create_dataset('label/length',
                                                    dtype='int32', shape=(0,),
                                                    maxshape=(None,))
        event = biglist(data_handle=event_h, max_len=FLAGS.MAXLEN)
        event_length = biglist(data_handle=event_length_h, max_len=FLAGS.MAXLEN)
        label = biglist(data_handle=label_h, max_len=FLAGS.MAXLEN)
        label_length = biglist(data_handle=label_length_h, max_len=FLAGS.MAXLEN)
        count = 0
        file_count = 0
        for root, dirs, files in os.walk(data_dir, topdown=False):
           for name in files:
            if name.endswith(".signal"):
                file_pre = os.path.splitext(name)[0]
                signal_f = os.path.join(root,name)
                f_signal = read_signal(signal_f,normalize = FLAGS.sig_norm)
                label_f = os.path.join(root,file_pre+'.label')
                if len(f_signal) == 0:
                    continue
                try:
                    f_label = read_label(label_f,
                                         skip_start=skip_start,
                                         window_n=int((k_mer - 1) / 2))
                except:
                    sys.stdout.write("Read the label %s fail.Skipped." % (name))
                    continue
                try:
                    tmp_event, tmp_event_length, tmp_label, tmp_label_length = read_raw(f_signal, f_label, seq_length)
                except Exception as e:
                    print("Extract label from %s fail, label position exceed max signal length."%(label_f))
                    raise e
                event += tmp_event
                event_length += tmp_event_length
                label += tmp_label
                label_length += tmp_label_length
                del tmp_event
                del tmp_event_length
                del tmp_label
                del tmp_label_length
                count = len(event)
                if file_count % 10 == 0:
                    if max_segments_num is not None:
                        count_bar.update(0,progress = count,total = max_segments_num)
                        count_bar.update_bar()
                        if len(event) > max_segments_num:
                            event.resize(max_segments_num)
                            label.resize(max_segments_num)
                            event_length.resize(max_segments_num)
    
                            label_length.resize(max_segments_num)
                            break
                    else:
                        count_bar.update(0,progress = count,total = count)
                        count_bar.update_bar()
                file_count += 1
        if event.cache:
            event.save_rest()
            event_length.save_rest()
            label.save_rest()
            label_length.save_rest()
            train = read_cache_dataset(h5py_file_path)
        else:
            event.save()
            event_length.save()
            label.save()
            label_length.save()
            train = read_cache_dataset(h5py_file_path)
        count_bar.end()
    return train


def read_signal(file_path, normalize=None):
    f_h = open(file_path, 'r')
    signal = list()
    for line in f_h:
        signal += [np.float32(x) for x in line.split()]
    signal = np.asarray(signal)
    if len(signal) == 0:
        return signal.tolist()
    if normalize == MEAN:
        signal = (signal - np.mean(signal)) / np.float(np.std(signal))
    elif normalize == MEDIAN:
        signal = (signal - np.median(signal)) / np.float(robust.mad(signal))
    return signal.tolist()

def read_signal_fast5(fast5_path, normalize=None):
    """
    Read signal from the fast5 file.
    TODO: To make it compatible with PromethION platform.
    """
    root = h5py.File(fast5_path, 'r')
    signal = np.asarray(list(root['/Raw/Reads'].values())[0][('Signal')])
    uniq_arr=np.unique(signal)
    if len(signal) == 0:
        return signal.tolist()
    if normalize == MEAN:
        signal = (signal - np.mean(uniq_arr)) / np.float(np.std(uniq_arr))
    elif normalize == MEDIAN:
        signal = (signal - np.median(uniq_arr)) / np.float(robust.mad(uniq_arr))
    return signal.tolist()
    
def read_signal_tfrecord(data_array, normalize=None):

    signal = data_array
    uniq_arr=np.unique(signal)
    if len(signal) == 0:
        return signal.tolist()
    if normalize == MEAN:
        signal = (signal - np.mean(uniq_arr)) / np.float(np.std(uniq_arr))
    elif normalize == MEDIAN:
        signal = (signal - np.median(uniq_arr)) / np.float(robust.mad(uniq_arr))
    return signal.tolist()


def read_label(file_path, skip_start=10, window_n=0):
    f_h = open(file_path, 'r')
    start = list()
    length = list()
    base = list()
    all_base = list()
    if skip_start < window_n:
        skip_start = window_n
    for line in f_h:
        record = line.split()
        all_base.append(base2ind(record[2]))
    f_h.seek(0, 0)  # Back to the start
    file_len = len(all_base)
    for count, line in enumerate(f_h):
        record = line.split()   
        if count < skip_start or count > (file_len - skip_start - 1):
            continue
        start.append(int(record[0]))
        length.append(int(record[1]) - int(record[0]))
        k_mer = 0
        for i in range(window_n * 2 + 1):
            k_mer = k_mer * 4 + all_base[count + i - window_n]
        base.append(k_mer)
    return raw_labels(start=start, length=length, base=base)


def read_label_tfrecord(raw_label_array, skip_start=10, window_n=0):
    """
    Args:
        raw_label_array: raw label string from tfrecord file.
        skip_start: Skip the first n label.
        window_n: If > 0, then a k-tuple nucleotide bases will be considered. 
    """
    start = list()
    length = list()
    base = list()
    all_base = list()
    count = 0
    window_n = int(window_n)
    if skip_start < window_n:
        skip_start = window_n
    for line in raw_label_array:
        if isinstance(line[2],bytes):
            c_base = line[2].decode()[2]
        else:
            c_base = line[2]
        all_base.append(base2ind(c_base))
    file_len = len(all_base)
    for count, line in enumerate(raw_label_array):
        if count < skip_start or count > (file_len - skip_start - 1):
            continue
        start.append(int(line[0]))
        length.append(int(line[1]) - int(line[0]))
        k_mer = 0
        for i in range(window_n * 2 + 1):
            k_mer = k_mer * 4 + all_base[count + i - window_n]
        base.append(k_mer)
    return raw_labels(start=start, length=length, base=base)


def read_raw(raw_signal, 
             raw_label, 
             max_seq_length):
    """
    Generate signal-label pair from the input raw signal and label.
    Args:
        raw_signal: 1d Vector contain the raw signal.
        raw_label:label data with start, length, base.
        max_seq_length: The segment length appointed by the training module.
    """
    label_val = list()
    label_length = list()
    event_val = list()
    event_length = list()
    current_length = 0
    current_label = []
    current_event = []
    signal_len = len(raw_signal)
    for indx, segment_length in enumerate(raw_label.length):
        current_start = raw_label.start[indx]
        current_base = raw_label.base[indx]
        if current_start+segment_length > signal_len:
            print(current_start)
            print(segment_length)
            print(signal_len)
            print(current_base)
            print(raw_signal[:200])
            print(raw_signal[-200:])
        assert(current_start+segment_length < signal_len)
        if current_length + segment_length < max_seq_length:
            current_event += raw_signal[current_start:current_start + segment_length]
            current_label.append(current_base)
            current_length += segment_length
        else:
            # Save current event and label, conduct a quality controle step of the label.
            if current_length > (max_seq_length * MIN_SIGNAL_PRO) and len(current_label) > MIN_LABEL_LENGTH:
                padding(current_event, max_seq_length,
                        raw_signal[
                        current_start + segment_length:current_start + segment_length + max_seq_length])
                event_val.append(current_event)
                event_length.append(current_length)
                label_val.append(current_label)
                label_length.append(len(current_label))
                # Begin a new event-label
            current_event = raw_signal[
                            current_start:current_start + segment_length]
            current_length = segment_length
            current_label = [current_base]
    return event_val, event_length, label_val, label_length


def padding(x, L, padding_list=None):
    """Padding the vector x to length L"""
    len_x = len(x)
    assert len_x <= L, "Length of vector x is larger than the padding length"
    zero_n = L - len_x
    if padding_list is None:
        x.extend([0] * zero_n)
    elif len(padding_list) < zero_n:
        x.extend(padding_list + [0] * (zero_n - len(padding_list)))
    else:
        x.extend(padding_list[0:zero_n])
    return None


def batch2sparse(label_batch):
    """Transfer a batch of label to a sparse tensor
    """
    values = []
    indices = []
    for batch_i, label_list in enumerate(label_batch[:, 0]):
        for indx, label in enumerate(label_list):
            if indx >= label_batch[batch_i, 1]:
                break
            indices.append([batch_i, indx])
            values.append(label)
    shape = [len(label_batch), max(label_batch[:, 1])]
    return indices, values, shape


def base2ind(base, alphabet_n=4, base_n=1):
    """base to 1-hot vector,
    Input Args:
        base: current base,can be AGCT, or AGCTX for methylation.
        alphabet_n: can be 4 or 5, related to normal DNA or methylation call.
        """
    if alphabet_n == 4:
        Alphabeta = ['A', 'C', 'G', 'T']
        alphabeta = ['a', 'c', 'g', 't']
    elif alphabet_n == 5:
        Alphabeta = ['A', 'C', 'G', 'T', 'X']
        alphabeta = ['a', 'c', 'g', 't', 'x']
    else:
        raise ValueError('Alphabet number should be 4 or 5.')
    if base.isdigit():
        return int(base) / 256
    if ord(base) < 97:
        return Alphabeta.index(base)
    else:
        return alphabeta.index(base)
    #

def test_chiron_dummy_input():
    DATA_FORMAT = np.dtype([('start','<i4'),
                            ('length','<i4'),
                            ('base','S1')]) 
    ### Generate dummy dataset and check input ###
    dummy_dir = './Dummy_data/'
    if not os.path.isdir(dummy_dir):
        os.makedirs(dummy_dir)
    dummy_fast5 = os.path.join(dummy_dir,'fast5s')
    dummy_raw = os.path.join(dummy_dir,'raw')
    if not os.path.isdir(dummy_fast5):
        os.makedirs(dummy_fast5)
    file_num = 10
    base_signal = {'A':100,'C':200,'G':300,'T':400}
    bases = ['A','C','G','T']
    for i in range(file_num):
        file_n = os.path.join(dummy_fast5,'dummy_' + str(i) + '.fast5')
        length = np.random.randint(40000,50000)
        start = 0
        start_list = []
        length_list = []
        base_list = []
        raw_signal = []
        while start < length-1:
            start_list.append(start)
            step = min(length-start-1, np.random.randint(5,150))
            length_list.append(step)
            start = start + step
            base = bases[np.random.randint(len(bases))]
            base_list.append(base)
            raw_signal = raw_signal + [base_signal[base]] + [base_signal[base]-1]*(step-1)
        event_matrix = np.asarray(list(zip(start_list,length_list,base_list)),dtype = DATA_FORMAT)
        with h5py.File(file_n,'w') as root:
            if '/Raw' in root:
                del root['/Raw']
            raw_h = root.create_dataset('/Raw/Reads/Read_'+ str(i)+'/Signal',
                                        shape = (len(raw_signal),),
                                        dtype = np.int16)
            channel_h=root.create_dataset('/UniqueGlobalKey/channel_id/',shape=[],dtype=np.int16)
            channel_h.attrs['offset']=0
            channel_h.attrs['range']=1
            channel_h.attrs['digitisation']=1
            raw_h[...] = raw_signal[::-1]
            if '/Analyses' in root:
                del root['/Analyses']
            event_h = root.create_dataset('/Analyses/Corrected_000/BaseCalled_template/Events', 
                                          shape = (len(event_matrix),),
                                          maxshape=(None,),
                                          dtype = DATA_FORMAT)
            event_h[...] = event_matrix
            event_h.attrs['read_start_rel_to_raw'] = 0
            
    class Args(object):
        def __init__(self):
            self.input = dummy_fast5
            self.output = dummy_raw
            self.basecall_group = 'Corrected_000'
            self.mode = 'rna'
            self.batch = 1
            self.basecall_subgroup = 'BaseCalled_template'
            self.unit=True
            self.min_bps = 0
            self.n_errors = 5
    from chiron.utils import raw
    args = Args()
    raw.run(args)
    train = read_raw_data_sets(dummy_raw,seq_length=1000,h5py_file_path=os.path.join(dummy_dir,'cache.fast5'))
    
    for i in range(100):
        inputX, sequence_length, label = train.next_batch(10,shuffle=False)
        accum_len = 0
        for idx,x in enumerate(inputX):
            x = inputX[idx][:sequence_length[idx]]
            y = list()
            for x_idx, signal in enumerate(x):
                if x_idx==0:
                    y.append(signal)
                else:
                    if (abs(signal - x[x_idx-1]) >0.1) or (signal - x[x_idx-1])>0:
                        y.append(signal)
            corr = np.corrcoef(y, label[1][accum_len:accum_len + len(y)])[0, 1]
            for loc in label[0][accum_len:accum_len + len(y)]:
                assert(loc[0] == idx)
            accum_len += len(y)
            assert abs(corr - 1)< 1e-6
    print("Input pipeline dummy data test passed!")
                    
#
if __name__ == '__main__':
    test_chiron_dummy_input()
#    TEST_DIR='/home/heavens/Documents/test/'
#    train = read_tfrecord(TEST_DIR,"train.tfrecords",seq_length=1000,h5py_file_path=os.path.join(TEST_DIR,'cache.fast5'))
