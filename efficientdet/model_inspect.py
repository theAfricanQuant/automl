# Copyright 2020 Google Research. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
r"""Tool to inspect a model."""

from __future__ import absolute_import
from __future__ import division
# gtype import
from __future__ import print_function

import os
import time

from absl import flags
import numpy as np
import tensorflow.compat.v1 as tf
from typing import Text, Tuple, List

import efficientdet_arch
import retinanet_arch
import utils


flags.DEFINE_string('model_name', 'efficientdet-d1', 'Model.')
flags.DEFINE_string('logdir', '/tmp/deff/', 'log directory.')
flags.DEFINE_string('runmode', 'dry', 'Run mode: {freeze, bm, dry}')
flags.DEFINE_string('trace_filename', None, 'Trace file name.')
flags.DEFINE_integer('num_classes', 90, 'Number of classes.')
flags.DEFINE_integer('input_image_size', 640, 'Size of input image.')
flags.DEFINE_integer('threads', 0, 'Number of threads.')
flags.DEFINE_integer('bm_runs', 20, 'Number of benchmark runs.')
flags.DEFINE_string('tensorrt', None, 'TensorRT mode: {None, FP32, FP16, INT8}')
flags.DEFINE_bool('delete_logdir', True, 'Whether to delete logdir.')
flags.DEFINE_bool('freeze', False, 'Freeze graph.')
flags.DEFINE_bool('xla', False, 'Run with xla optimization.')

flags.DEFINE_string('ckpt_path', None, 'checkpoint dir used for eval.')
flags.DEFINE_string('export_ckpt', None, 'Path for exporting new models.')
flags.DEFINE_bool('enable_ema', True, 'Use ema variables for eval.')

FLAGS = flags.FLAGS


class ModelInspector(object):
  """A simple helper class for inspecting a model."""

  def __init__(self,
               model_name: Text,
               image_size: int,
               num_classes: int,
               logdir: Text,
               tensorrt: Text = False,
               use_xla: bool = False,
               ckpt_path: Text = None,
               enable_ema: bool = True,
               export_ckpt: Text = None):
    self.model_name = model_name
    self.image_size = image_size
    self.logdir = logdir
    self.tensorrt = tensorrt
    self.use_xla = use_xla
    self.ckpt_path = ckpt_path
    self.enable_ema = enable_ema
    self.export_ckpt = export_ckpt

    # A few fixed parameters.
    batch_size = 1
    self.num_classes = num_classes
    self.inputs_shape = [batch_size, image_size, image_size, 3]
    self.labels_shape = [batch_size, self.num_classes]

  def build_model(self, inputs: tf.Tensor,
                  is_training: bool) -> List[tf.Tensor]:
    """Build model with inputs and labels and print out model stats."""
    tf.logging.info('start building model')
    if self.model_name.startswith('efficientdet'):
      cls_outputs, box_outputs = efficientdet_arch.efficientdet(
          inputs,
          model_name=self.model_name,
          is_training_bn=is_training,
          use_bfloat16=False)
    elif self.model_name.startswith('retinanet'):
      cls_outputs, box_outputs = retinanet_arch.retinanet(
          inputs,
          model_name=self.model_name,
          is_training_bn=is_training,
          use_bfloat16=False)

    print('backbone+fpn+box params/flops = {:.6f}M, {:.9f}B'.format(
        *utils.num_params_flops()))

    return list(cls_outputs.values()) + list(box_outputs.values())

  def build_and_save_model(self):
    """build and save the model into self.logdir."""
    with (tf.Graph().as_default(), tf.Session() as sess):
      # Build model with inputs and labels.
      inputs = tf.placeholder(tf.float32, name='input', shape=self.inputs_shape)
      outputs = self.build_model(inputs, is_training=False)

      # Run the model
      inputs_val = np.random.rand(*self.inputs_shape).astype(float)
      labels_val = np.zeros(self.labels_shape).astype(np.int64)
      labels_val[:, 0] = 1
      sess.run(tf.global_variables_initializer())
      # Run a single train step.
      sess.run(outputs, feed_dict={inputs: inputs_val})
      all_saver = tf.train.Saver(save_relative_paths=True)
      all_saver.save(sess, os.path.join(self.logdir, self.model_name))

      tf_graph = os.path.join(self.logdir, f'{self.model_name}_train.pb')
      with tf.io.gfile.GFile(tf_graph, 'wb') as f:
        f.write(sess.graph_def.SerializeToString())

  def restore_model(self, sess, ckpt_path, enable_ema=True, export_ckpt=None):
    """Restore variables from a given checkpoint."""
    sess.run(tf.global_variables_initializer())
    checkpoint = tf.train.latest_checkpoint(ckpt_path)
    if enable_ema:
      ema = tf.train.ExponentialMovingAverage(decay=0.0)
      ema_vars = utils.get_ema_vars()
      var_dict = ema.variables_to_restore(ema_vars)
      ema_assign_op = ema.apply(ema_vars)
    else:
      var_dict = utils.get_ema_vars()
      ema_assign_op = None

    tf.train.get_or_create_global_step()
    sess.run(tf.global_variables_initializer())
    saver = tf.train.Saver(var_dict, max_to_keep=1)
    saver.restore(sess, checkpoint)

    if export_ckpt:
      print(f'export model to {export_ckpt}')
      if ema_assign_op is not None:
        sess.run(ema_assign_op)
      saver = tf.train.Saver(max_to_keep=1, save_relative_paths=True)
      saver.save(sess, export_ckpt)

  def eval_ckpt(self):
    """build and save the model into self.logdir."""
    with tf.Graph().as_default(), tf.Session() as sess:
      # Build model with inputs and labels.
      inputs = tf.placeholder(tf.float32, name='input', shape=self.inputs_shape)
      self.build_model(inputs, is_training=False)
      self.restore_model(
          sess, self.ckpt_path, self.enable_ema, self.export_ckpt)

  def freeze_model(self) -> Tuple[Text, Text]:
    """Freeze model and convert them into tflite and tf graph."""
    with (tf.Graph().as_default(), tf.Session() as sess):
      inputs = tf.placeholder(tf.float32, name='input', shape=self.inputs_shape)
      outputs = self.build_model(inputs, is_training=False)

      checkpoint = tf.train.latest_checkpoint(self.logdir)
      tf.logging.info(f'Loading checkpoint: {checkpoint}')
      saver = tf.train.Saver()

      # Restore the Variables from the checkpoint and freeze the Graph.
      saver.restore(sess, checkpoint)

      output_node_names = [node.name.split(':')[0] for node in outputs]
      graphdef = tf.graph_util.convert_variables_to_constants(
          sess, sess.graph_def, output_node_names)

    return graphdef

  def benchmark_model(self, warmup_runs, bm_runs, num_threads,
                      trace_filename=None):
    """Benchmark model."""
    if self.tensorrt:
      print('Using tensorrt ', self.tensorrt)
      self.build_and_save_model()
      graphdef = self.freeze_model()

    if num_threads > 0:
      print(f'num_threads for benchmarking: {num_threads}')
      sess_config = tf.ConfigProto(
          intra_op_parallelism_threads=num_threads,
          inter_op_parallelism_threads=1)
    else:
      sess_config = tf.ConfigProto()

    # rewriter_config_pb2.RewriterConfig.OFF
    sess_config.graph_options.rewrite_options.dependency_optimization = 2
    if self.use_xla:
      sess_config.graph_options.optimizer_options.global_jit_level = (
          tf.OptimizerOptions.ON_2)

    with (tf.Graph().as_default(), tf.Session(config=sess_config) as sess):
      inputs = tf.placeholder(tf.float32, name='input', shape=self.inputs_shape)
      output = self.build_model(inputs, is_training=False)

      img = np.random.uniform(size=self.inputs_shape)

      sess.run(tf.global_variables_initializer())
      if self.tensorrt:
        fetches = [inputs.name] + [i.name for i in output]
        goutput = self.convert_tr(graphdef, fetches)
        inputs, output = goutput[0], goutput[1:]

      if not self.use_xla:
        # Don't use tf.group because XLA removes the whole graph for tf.group.
        output = tf.group(*output)
      for i in range(warmup_runs):
        start_time = time.time()
        sess.run(output, feed_dict={inputs: img})
        print('Warm up: {} {:.4f}s'.format(i, time.time() - start_time))
      print(f'Start benchmark runs total={bm_runs}')
      timev = []
      for i in range(bm_runs):
        if trace_filename and i == (bm_runs // 2):
          run_options = tf.RunOptions()
          run_options.trace_level = tf.RunOptions.FULL_TRACE
          run_metadata = tf.RunMetadata()
          sess.run(output, feed_dict={inputs: img},
                   options=run_options, run_metadata=run_metadata)
          tf.logging.info(f'Dumping trace to {trace_filename}')
          trace_dir = os.path.dirname(trace_filename)
          if not tf.io.gfile.exists(trace_dir):
            tf.io.gfile.makedirs(trace_dir)
          with tf.io.gfile.GFile(trace_filename, 'w') as trace_file:
            from tensorflow.python.client import timeline  # pylint: disable=g-direct-tensorflow-import,g-import-not-at-top
            trace = timeline.Timeline(step_stats=run_metadata.step_stats)
            trace_file.write(
                trace.generate_chrome_trace_format(show_memory=True))

        start_time = time.time()
        sess.run(output, feed_dict={inputs: img})
        timev.append(time.time() - start_time)

      timev.sort()
      timev = timev[2:bm_runs-2]
      print('{} {}runs {}threads: mean {:.4f} std {:.4f} min {:.4f} max {:.4f}'
            .format(self.model_name, len(timev), num_threads, np.mean(timev),
                    np.std(timev), np.min(timev), np.max(timev)))

  def convert_tr(self, graph_def, fetches):
    """Convert to TensorRT."""
    from tensorflow.python.compiler.tensorrt import trt  # pylint: disable=g-direct-tensorflow-import,g-import-not-at-top
    converter = trt.TrtGraphConverter(
        nodes_blacklist=[t.split(':')[0] for t in fetches],
        input_graph_def=graph_def,
        precision_mode=self.tensorrt)
    infer_graph = converter.convert()
    return tf.import_graph_def(infer_graph, return_elements=fetches)

  def run_model(self, runmode, threads=0):
    """Run the model on devices."""
    if runmode == 'dry':
      self.build_and_save_model()
    elif runmode == 'freeze':
      self.build_and_save_model()
      self.freeze_model()
    elif runmode == 'ckpt':
      self.eval_ckpt()
    elif runmode == 'bm':
      self.benchmark_model(warmup_runs=5, bm_runs=FLAGS.bm_runs,
                           num_threads=threads,
                           trace_filename=FLAGS.trace_filename)


def main(_):
  if tf.io.gfile.exists(FLAGS.logdir) and FLAGS.delete_logdir:
    tf.logging.info('Deleting log dir ...')
    tf.io.gfile.rmtree(FLAGS.logdir)

  inspector = ModelInspector(
      model_name=FLAGS.model_name,
      image_size=FLAGS.input_image_size,
      num_classes=FLAGS.num_classes,
      logdir=FLAGS.logdir,
      tensorrt=FLAGS.tensorrt,
      use_xla=FLAGS.xla,
      ckpt_path=FLAGS.ckpt_path,
      enable_ema=FLAGS.enable_ema,
      export_ckpt=FLAGS.export_ckpt)
  inspector.run_model(FLAGS.runmode, FLAGS.threads)


if __name__ == '__main__':
  tf.app.run(main)
