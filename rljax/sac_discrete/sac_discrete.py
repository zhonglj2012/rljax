from functools import partial
from typing import Tuple

import numpy as np

import jax
import jax.numpy as jnp
from flax import nn, optim
from rljax.common.base_class import DiscreteOffPolicyAlgorithm
from rljax.common.utils import soft_update, update_network
from rljax.sac.network import build_sac_log_alpha
from rljax.sac_discrete.network import build_sac_discrete_actor, build_sac_discrete_critic


def critic_grad_fn(
    actor: nn.Model,
    critic: nn.Model,
    critic_target: nn.Model,
    log_alpha: nn.Model,
    gamma: float,
    state: jnp.ndarray,
    action: jnp.ndarray,
    reward: jnp.ndarray,
    done: jnp.ndarray,
    next_state: jnp.ndarray,
) -> nn.Model:
    alpha = jax.lax.stop_gradient(jnp.exp(log_alpha()))
    pi, log_pi = actor(next_state)
    next_q1, next_q2 = critic_target(next_state)
    next_q = (pi * (jnp.minimum(next_q1, next_q2) - alpha * log_pi)).sum(axis=1, keepdims=True)
    target_q = jax.lax.stop_gradient(reward + (1.0 - done) * gamma * next_q)

    def _loss(action, curr_q1, curr_q2, target_q):
        return jnp.square(target_q - curr_q1[action]) + jnp.square(target_q - curr_q2[action])

    def critic_loss_fn(critic):
        curr_q1, curr_q2 = critic(state)
        return jax.vmap(_loss)(action, curr_q1, curr_q2, target_q).mean()

    grad_critic = jax.grad(critic_loss_fn)(critic)
    return grad_critic


def actor_and_alpha_grad_fn(
    actor: nn.Model,
    critic: nn.Model,
    log_alpha: nn.Model,
    target_entropy: float,
    state: jnp.ndarray,
) -> Tuple[nn.Model, nn.Model]:
    alpha = jax.lax.stop_gradient(jnp.exp(log_alpha()))
    curr_q1, curr_q2 = critic(state)
    curr_q = jax.lax.stop_gradient(jnp.minimum(curr_q1, curr_q2))

    def actor_loss_fn(actor):
        pi, log_pi = actor(state)
        mean_log_pi = (pi * log_pi).sum(axis=1).mean()
        mean_q = (pi * curr_q).sum(axis=1).mean()
        loss_actor = alpha * mean_log_pi - mean_q
        return loss_actor, mean_log_pi

    grad_actor, mean_log_pi = jax.grad(actor_loss_fn, has_aux=True)(actor)
    mean_log_pi = jax.lax.stop_gradient(mean_log_pi)

    def alpha_loss_fn(log_alpha):
        loss_alpha = -log_alpha() * (target_entropy + mean_log_pi)
        return loss_alpha

    grad_alpha = jax.grad(alpha_loss_fn)(log_alpha)
    return grad_actor, grad_alpha


class SACDiscrete(DiscreteOffPolicyAlgorithm):
    def __init__(
        self,
        state_space,
        action_space,
        seed,
        gamma=0.99,
        buffer_size=10 ** 6,
        batch_size=256,
        start_steps=1000,
        update_interval=1,
        update_interval_target=1000,
        lr_actor=3e-4,
        lr_critic=3e-4,
        lr_alpha=3e-4,
        units_actor=(512,),
        units_critic=(512,),
    ):
        super(SACDiscrete, self).__init__(
            state_space=state_space,
            action_space=action_space,
            seed=seed,
            gamma=gamma,
            buffer_size=buffer_size,
            batch_size=batch_size,
            start_steps=start_steps,
            update_interval=update_interval,
            update_interval_target=update_interval_target,
        )

        # Actor.
        actor = build_sac_discrete_actor(
            state_dim=state_space.shape[0],
            action_dim=action_space.n,
            rng_init=next(self.rng),
            hidden_units=units_actor,
        )
        self.optim_actor = jax.device_put(optim.Adam(learning_rate=lr_actor).create(actor))

        # Critic.
        rng_critic = next(self.rng)
        critic = build_sac_discrete_critic(
            state_dim=state_space.shape[0],
            action_dim=action_space.n,
            rng_init=rng_critic,
            hidden_units=units_critic,
        )
        self.optim_critic = jax.device_put(optim.Adam(learning_rate=lr_critic).create(critic))

        # Target network.
        self.critic_target = jax.device_put(
            build_sac_discrete_critic(
                state_dim=state_space.shape[0],
                action_dim=action_space.n,
                rng_init=rng_critic,
                hidden_units=units_critic,
            )
        )

        # Entropy coefficient.
        target_entropy = -np.log(1.0 / action_space.n) * 0.98
        log_alpha = build_sac_log_alpha(next(self.rng))
        self.optim_alpha = jax.device_put(optim.Adam(learning_rate=lr_alpha).create(log_alpha))

        # Compile functions.
        self.critic_grad_fn = jax.jit(partial(critic_grad_fn, gamma=gamma))
        self.actor_and_alpha_grad_fn = jax.jit(partial(actor_and_alpha_grad_fn, target_entropy=target_entropy))

    def select_action(self, state):
        state = jax.device_put(state[None, ...])
        pi, _ = self.actor(state)
        return np.argmax(pi)

    def explore(self, state):
        state = jax.device_put(state[None, ...])
        pi, _ = self.actor(state)
        action = jax.random.categorical(next(self.rng), pi)
        return np.array(action[0])

    def step(self, env, state, t, step):
        t += 1

        if step <= self.start_steps:
            action = env.action_space.sample()
        else:
            action = self.explore(state)

        next_state, reward, done, _ = env.step(action)
        mask = False if t == env._max_episode_steps else done
        self.buffer.append(state, action, reward, mask, next_state)

        if done:
            t = 0
            next_state = env.reset()

        return next_state, t

    def update(self):
        self.learning_steps += 1
        state, action, reward, done, next_state = self.buffer.sample(self.batch_size)

        # Update critic.
        grad_critic = self.critic_grad_fn(
            actor=self.actor,
            critic=self.critic,
            critic_target=self.critic_target,
            log_alpha=self.log_alpha,
            state=state,
            action=action,
            reward=reward,
            done=done,
            next_state=next_state,
        )
        self.optim_critic = update_network(self.optim_critic, grad_critic)

        # Update actor and log alpha.
        grad_actor, grad_alpha = self.actor_and_alpha_grad_fn(
            actor=self.actor,
            critic=self.critic,
            log_alpha=self.log_alpha,
            state=state,
        )
        self.optim_actor = update_network(self.optim_actor, grad_actor)
        self.optim_alpha = update_network(self.optim_alpha, grad_alpha)

        # Update target network.
        if (self.learning_steps * self.update_interval) % self.update_interval_target == 0:
            self.critic_target = soft_update(self.critic_target, self.critic, 1.0)

    @property
    def actor(self):
        return self.optim_actor.target

    @property
    def critic(self):
        return self.optim_critic.target

    @property
    def log_alpha(self):
        return self.optim_alpha.target