import argparse
import importlib
import shutil
import pandas as pd
from const import *
from config import *
from layers import *

tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.ERROR)
def parse_example_helper_csv(line ):
    columns = tf.io.decode_csv( [line], record_defaults = CSV_RECORD_DEFAULTS )

    features = dict( zip( FEATURE_NAME, columns ) )

    target = tf.reshape( tf.cast( tf.equal( features.pop( TARGET  ), TARGET_VAL ), tf.float32 ), [-1] )

    return features, target

def parse_example_helper_libsvm(line):
    # '0 1:0 2:0.053068 3:0.5 4:0.1 5:0.113437 6:0.874'
    columns = tf.string_split([line], ' ')

    target = tf.string_to_number(columns.values[0], out_type = tf.float32)
    target = tf.reshape(tf.cast( tf.equal( target, 1), tf.float32), [-1])

    splits = tf.string_split(columns.values[1:], ':')
    id_vals = tf.reshape(splits.values, splits.dense_shape )

    feat_ids, feat_vals = tf.split(id_vals, num_or_size_splits =2, axis=1)
    feat_ids = tf.string_to_number(feat_ids , out_type = tf.int32)
    feat_vals = tf.string_to_number(feat_vals, out_type = tf.float32)

    return {'feat_ids': feat_ids, 'feat_vals': feat_vals}, target


def parse_example_helper_tfreocrd(line):
    features = tf.parse_single_example(line, features = AMAZON_PROTO)

    for i in AMAZON_VARLEN:
        features[i] = tf.sparse_tensor_to_dense(features[i])

    target = tf.reshape(tf.cast( features.pop( AMAZON_TARGET ), tf.float32),[-1])

    return features, target

def input_fn(step, is_predict, config):
    def func():
        if config.input_parser == 'csv':
            dataset = tf.data.TextLineDataset(config.data_dir.format(step)) \
            .skip( 1 ) \
            .map( parse_example_helper_csv, num_parallel_calls=8 )

        elif config.input_parser == 'libsvm':
            dataset = tf.data.TextLineDataset( config.data_dir.format(step) ) \
                .skip( 1 ) \
                .map( parse_example_helper_libsvm, num_parallel_calls=8 )

        elif config.input_parser == 'tfrecord':
            dataset = tf.data.TFRecordDataset( config.data_dir.format(step)) \
                .map( parse_example_helper_tfreocrd, num_parallel_calls=8 )

        else:
            raise Exception('Only [csv|libsvm|tfrecord] are supported now')

        if not is_predict:
            # shuffle before repeat and batch last
            dataset = dataset \
                .shuffle(MODEL_PARAMS['buffer_size'] ) \
                .repeat(MODEL_PARAMS['num_epochs'] ) \

        if 'varlen' in config.input_type:
            dataset = dataset\
                .padded_batch(batch_size = MODEL_PARAMS['batch_size'] ,
                              padded_shapes = config.pad_shape)
        else:
            dataset = dataset \
                .batch(MODEL_PARAMS['batch_size'] )

        return dataset
    return func


def add_layer_summary(tag, value):
  tf.summary.scalar('{}/fraction_of_zero_values'.format(tag), tf.math.zero_fraction(value))
  tf.summary.histogram('{}/activation'.format(tag),  value)


def tf_estimator_model(model_fn):
    def model_fn_helper(features, labels, mode, params):

        y = model_fn(features , labels, mode, params)

        add_layer_summary('label_mean', labels)

        if mode == tf.estimator.ModeKeys.PREDICT:
            predictions = {
                'prediction_prob': tf.sigmoid( y )
            }
            return tf.estimator.EstimatorSpec( mode=tf.estimator.ModeKeys.PREDICT,
                                               predictions=predictions )

        cross_entropy = tf.reduce_mean( tf.nn.sigmoid_cross_entropy_with_logits( labels=labels, logits=y ) )

        if mode == tf.estimator.ModeKeys.TRAIN:
            optimizer = tf.train.AdagradOptimizer( learning_rate=params['learning_rate'] )
            update_ops = tf.get_collection( tf.GraphKeys.UPDATE_OPS )
            with tf.control_dependencies( update_ops ):
                train_op = optimizer.minimize( cross_entropy,
                                               global_step=tf.train.get_global_step() )
            return tf.estimator.EstimatorSpec( mode, loss=cross_entropy, train_op=train_op )
        else:
            eval_metric_ops = {
                'accuracy': tf.metrics.accuracy( labels=labels,
                                                 predictions=tf.to_float(tf.greater_equal(tf.sigmoid(y),0.5))  ),
                'auc': tf.metrics.auc( labels=labels,
                                       predictions=tf.sigmoid( y )),
                'pr': tf.metrics.auc( labels=labels,
                                      predictions=tf.sigmoid( y ),
                                      curve='PR' )
            }
            return tf.estimator.EstimatorSpec( mode, loss=cross_entropy, eval_metric_ops=eval_metric_ops )

    return model_fn_helper


def build_estimator_helper(model_fn, params):
    def build_estimator(config):

        if config.data_name not in model_fn:
            raise Exception('Only [{}] are supported'.format(','.join(model_fn.keys()) ))

        run_config = tf.estimator.RunConfig(
            save_summary_steps=50,
            log_step_count_steps=50,
            keep_checkpoint_max = 3,
            save_checkpoints_steps =50
        )

        if 'model_type' in params:
            # PNN -> PNN/IPNN
            # FiBiNET -> field_all/field_each/field_interaction
            # EMMLP -> dense/bucketize
            model_dir = config.checkpoint_dir + '/' + params['model_type']
        else:
            model_dir = config.checkpoint_dir

        estimator = tf.estimator.Estimator(
            model_fn = model_fn[config.data_name],
            config = run_config,
            params = params[config.data_name],
            model_dir = model_dir
        )

        return estimator
    return build_estimator


def main(args):
    model = args.model
    config = CONFIG( model_name = model, data_name = args.dataset )

    # clear Existing Model
    if args.clear_model:
        try:
            shutil.rmtree(config.checkpoint_dir)
        except Exception as e:
            print('Error! {} occured at model cleaning'.format(e))
        else:
            print( '{} model cleaned'.format(config.checkpoint_dir) )

    # build estimator
    build_estimator = getattr(importlib.import_module('model.{}.{}'.format(model, model)),
                             'build_estimator')
    estimator = build_estimator(config)

    # train or predict
    if args.step == 'train':
        early_stopping = tf.estimator.experimental.stop_if_no_decrease_hook(
            estimator,
            metric_name="loss",
            max_steps_without_decrease= 20 * 100 )

        train_spec = tf.estimator.TrainSpec( input_fn = input_fn( step = 'train',
                                             is_predict = 0,
                                            config = config), hooks = [early_stopping])

        eval_spec = tf.estimator.EvalSpec( input_fn = input_fn( step ='valid',
                                           is_predict = 1,
                                           config = config ),
                                           steps = 200,
                                           throttle_secs = 60)

        tf.estimator.train_and_evaluate( estimator, train_spec, eval_spec)

    if args.step =='predict':
        prediction = estimator.predict( input_fn = input_fn( step='valid',
                                        is_predict = 1,
                                        config = config) )

        predict_prob = pd.DataFrame({'predict_prob': [i['prediction_prob'][1] for i in prediction ]})
        predict_prob.to_csv('./result/prediction_{}.csv'.format(model))


if __name__ =='__main__':
    # python train_seq.py --model DIN --step train --dataset amazon --clear_model 1

    parser = argparse.ArgumentParser()
    parser.add_argument( '--model', type = str, help = 'which model to use[FM|FFM]', required=True )
    parser.add_argument( '--step', type = str, help = 'Train or Predict', required=False, default='train' )
    parser.add_argument( '--clear_model', type=int, help= 'Whether to clear existing model', required=False, default=1)
    parser.add_argument( '--dataset', type=str, help= 'which dataset to use [frappe, census, amazon]',
                         required=False, default='dense')
    args = parser.parse_args()

    main(args)
