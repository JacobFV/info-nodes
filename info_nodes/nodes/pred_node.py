import math
import statistics
from typing import Optional, List, Text, Mapping

import tensorflow as tf
import tf_agents.typing.types as ts

from .info_node import InfoNode
from ..utils import keys

keras = tf.keras
unpacked_ave = statistics.mean  # special name just to make clear when I am wanting the average of an unpacked nest


class PredNode(InfoNode):
    """
    Predictive `InfoNode`

    f_abs, f_pred, and f_act must all output a `convert_to_tensor` distribution that can
    be sampled and entropy be calculated. All three functions are trained with Keras'
    `.fit` function using `negloglik`.
    """

    def __init__(self,
        f_abs: keras.Model,
        f_pred: keras.Model,
        f_act: keras.Model,
        parent_names: List[Text],
        neighbor_transl: Mapping[Text, keras.Model],
        num_children: ts.Int,
        latent_spec: ts.NestedTensorSpec,
        use_predictive_coding: bool = True,  # don't change this
        name: Text = 'PredNode'):

        self.f_abs = f_abs
        self.f_pred = f_pred
        self.f_act = f_act
        self.neighbor_transl = neighbor_transl
        self._use_predictive_coding = use_predictive_coding

        super(PredNode, self).__init__(
            state_spec_extras={
                keys.PRED_LATENT: latent_spec,
                keys.PRED_ENERGY: tf.TensorSpec((1,))
            },
            controllable_latent_spec=latent_spec,
            latent_spec=latent_spec,
            parent_names=parent_names,
            num_children=num_children,
            subnodes=list(),
            name=name
        )

    def bottom_up(self, states: Mapping[Text, ts.NestedTensor]) -> Mapping[Text, ts.NestedTensor]:

        # get new latent
        latent_dist = self.f_abs(dict(latent=states[self.name][keys.LATENT],
                                      parent_latents={name: states[name][keys.LATENT]
                                                      for name in self.parent_names}))
        latent_sample = latent_dist.sample()

        # determine uncertainty
        states[self.name][keys.ENERGY] = latent_dist.entropy() + -latent_dist.log_prob(latent_sample) \
                                         + unpacked_ave([states[name][keys.ENERGY]
                                                                 for name in self.parent_names])

        if self._use_predictive_coding:
            latent_sample = tf.nest.map_structure((lambda x,y: x-y),
                                                  states[self.name][keys.PRED_LATENT],
                                                  latent_sample)

        # psuedo-KL divergence loss
        states[self.name][keys.ENERGY] = sum(tf.nest.map_structure((lambda x, y: tf.norm(x - y, 1)),
                                                                   states[self.name][keys.LATENT],
                                                                   latent_sample))

        # update self state
        states[self.name][keys.LATENT] = latent_sample

        # return updated states
        return states

    def top_down(self, states: Mapping[Text, ts.NestedTensor]) -> Mapping[Text, ts.NestedTensor]:

        # cluster to neighbors if there is a cluster
        '''Attract to cluster of neighbor latents only if inside the IQR
        For now, simple attraction places the latent halfway between its
        current position and the local mean.'''
        # TODO add probablistic energy logit weighting to centroid influence
        self_latent = states[self.name][keys.LATENT]
        latent_points = [tf.nest.flatten(f_transl(
                            dict(latent=states[name][keys.LATENT],
                                 uncertainty=states[name][keys.ENERGY])).sample())
                         for name, f_transl in self.neighbor_transl.items()]
        latent_points.append(self_latent)
        latent_centroid = [sum(datum) / len(datum) for datum in zip(*latent_points)]
        # L1 distance for nested tensors
        def L1(x: List, y: List):
            return sum(abs(xi - yi) for xi, yi in zip(x, y)) / len(x)
        dists = [L1(latent, latent_centroid)
                 for latent in latent_points]
        dists_mean = statistics.mean(dists)
        dists_stddev = statistics.stdev(data=dists, xbar=dists_mean)
        zscore = dists[-1] / dists_stddev
        beta = tf.exp(-dists_mean*zscore)
        # I am not entirely sure multiplying by `dists_mean` will help spot high entropy scenerios
        # gravitate towards center
        def beta_update(old, new):
            return beta * old + (1-beta) * new
        states[self.name][keys.LATENT] = tf.nest.map_structure(beta_update, self_latent, latent_centroid)
        # update entropy after cluster gravitation
        def zscore2entropy(z):
            p = tf.exp(-(z**2)/2) / (2*math.pi)**0.5
            infomation = -tf.math.log(p)
            return infomation
        states[self.name][keys.ENERGY] = beta * states[self.name][keys.ENERGY] \
                                         + (1. - beta) * zscore2entropy(zscore)

        # self prediction
        predicted_latent_dist = self.f_pred(dict(latent=states[self.name][keys.LATENT],
                                                 entropy=states[self.name][keys.ENERGY]))
        predicted_latent = predicted_latent_dist.sample()
        states[self.name][keys.PRED_LATENT] = predicted_latent
        states[self.name][keys.PRED_ENERGY] = predicted_latent_dist.entropy() \
                                              + -predicted_latent_dist.log_prob(predicted_latent)

        # target sampling
        logits = [-target[0] for target in states[self.name][keys.TARGET_LATENTS]]
        logits.append(states[self.name][keys.PRED_ENERGY])
        values = [target[1] for target in states[self.name][keys.TARGET_LATENTS]]
        values.append(states[self.name][keys.PRED_LATENT])

        # TODO I know this doesn't exist, but I need it. I might just make it
        target_dist = WeightedEmperical(logits=logits, values=values)
        target_sample = target_dist.sample()

        # action generation
        action_dist = self.f_act(dict(
            latent=states[self.name][keys.LATENT],
            target=target_sample,
            latent_entropy=states[self.name][keys.ENERGY],
            target_entropy=target_dist.entropy()+-target_dist.log_prob(target_sample),
            parent_latents={name: states[name][keys.LATENT]
                            for name in self.parent_names}
        ))
        action_sample = action_dist.sample()
        action_energy = action_dist.entropy() + -action_dist.log_prob(action_sample)

        # assign parent targets
        for parent_name in self.parent_names:
            if parent_name in action_sample:
                slot_index = self._controllable_parent_slots[parent_name]
                states[parent_name][keys.TARGET_LATENTS][slot_index] = (action_energy, action_sample[parent_name])

        # TODO I should update loss here as well
        states[self.name][keys.ENERGY] += TODO

        return states
