import tensorflow as tf
from utils.census_ctr_feat_config import build_census_emb_columns, build_census_wide_columns

def deepfm_model_fn(features, labels, mode, params):
    emb_feat_columns, emb_field_size = build_census_emb_columns()
    wide_feat_columns, wide_field_size = build_census_wide_columns()
    emb_input_layer = tf.feature_column.input_layer(features=features, feature_columns=emb_feat_columns)

    with tf.name_scope('wide'):
        wide_input_layer = tf.feature_column.input_layer(features=features, feature_columns=wide_feat_columns)
        wide_output_layer = tf.layers.dense(inputs=wide_input_layer, units=1, activation=None, use_bias=True)

    with tf.name_scope('deep'):
        d_layer_1 = tf.layers.dense(inputs=emb_input_layer, units=50, activation=tf.nn.relu, use_bias=True)
        bn_layer_1 = tf.layers.batch_normalization(inputs=d_layer_1, axis=-1, momentum=0.99, epsilon=0.001, center=True, scale=True)
        deep_output_layer = tf.layers.dense(inputs=bn_layer_1, units=40, activation=tf.nn.relu, use_bias=True)

    with tf.name_scope('fm'):
        total_feat = tf.reshape(emb_input_layer, [-1, emb_field_size, 32])
        sum_square_part = tf.square(tf.reduce_sum(total_feat, 1))
        # print('sum_square_part:', sum_square_part.get_shape().as_list())
        square_sum_part = tf.reduce_sum(tf.square(total_feat), 1)
        # print('square_sum_part:', square_sum_part.get_shape().as_list())
        second_order_part = 0.5 * tf.subtract(sum_square_part, square_sum_part)
        # print('second_order_part:', second_order_part.get_shape().as_list())

    with tf.name_scope('concat'):
        m_layer = tf.concat([wide_output_layer, deep_output_layer, second_order_part], 1)
        o_layer = tf.layers.dense(inputs=m_layer, units=1, activation=None, use_bias=True)

    with tf.name_scope('logit'):
        o_prob = tf.nn.sigmoid(o_layer)
        predictions = tf.cast((o_prob > 0.5), tf.float32)

    labels = tf.cast(labels, tf.float32, name='true_label')
    # define loss
    loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(labels=labels, logits=o_layer))
    # evaluation
    accuracy = tf.metrics.accuracy(labels, predictions)
    auc = tf.metrics.auc(labels, predictions)
    my_metrics = {
        'accuracy': tf.metrics.accuracy(labels, predictions),
        'auc': tf.metrics.auc(labels, predictions)
    }
    tf.summary.scalar('accuracy', accuracy[1])
    tf.summary.scalar('auc', auc[1])

    # define train_op
    optimizer = tf.train.AdamOptimizer(learning_rate=0.0004, beta1=0.9, beta2=0.999)
    train_op = optimizer.minimize(loss=loss, global_step=tf.train.get_global_step())

    if mode == tf.estimator.ModeKeys.TRAIN:
        return tf.estimator.EstimatorSpec(mode, loss=loss, train_op=train_op)
    elif mode == tf.estimator.ModeKeys.EVAL:
        return tf.estimator.EstimatorSpec(
            mode, loss = loss, eval_metric_ops=my_metrics)
    elif mode == tf.estimator.ModeKeys.PREDICT:
        return tf.estimator.EstimatorSpec(mode, predictions=predictions)
    else:
        print('ERROR')