# -*- coding: utf-8 -*-
"""SAC agent from demonstration for episodic tasks in OpenAI Gym.

- Author: Curt Park
- Contact: curt.park@medipixel.io
- Paper: https://arxiv.org/pdf/1801.01290.pdf
         https://arxiv.org/pdf/1812.05905.pdf
         https://arxiv.org/pdf/1511.05952.pdf
         https://arxiv.org/pdf/1707.08817.pdf
"""

import argparse
import os
import pickle
from typing import Tuple

import gym
import numpy as np
import torch
import torch.optim as optim
import wandb

import algorithms.common.utils.helper_functions as common_utils
from algorithms.common.abstract.agent import AbstractAgent
from algorithms.common.replaybuffer.priortized_replay_buffer_fd import (
    PrioritizedReplayBufferfD,
)
from algorithms.sac.model import Actor, Qvalue, Value

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

# hyper parameters
hyper_params = {
    "GAMMA": 0.99,
    "TAU": 1e-3,
    "W_ENTROPY": 1e-3,
    "W_MEAN_REG": 1e-3,
    "W_STD_REG": 1e-3,
    "W_PRE_ACTIVATION_REG": 0.0,
    "LR_ACTOR": 3e-4,
    "LR_VF": 3e-4,
    "LR_QF1": 3e-4,
    "LR_QF2": 3e-4,
    "LR_ENTROPY": 3e-4,
    "DELAYED_UPDATE": 2,
    "BUFFER_SIZE": int(1e6),
    "BATCH_SIZE": 128,
    "AUTO_ENTROPY_TUNING": True,
    "PRETRAIN_STEP": 100,
    "MULTIPLE_LEARN": 1,  # multiple learning updates
    "LAMDA1": 1.0,  # N-step return weight
    "LAMDA2": 1e-5,  # l2 regularization weight
    "LAMDA3": 1.0,  # actor loss contribution of prior weight
    "PER_ALPHA": 0.5,
    "PER_BETA": 0.4,
    "PER_EPS": 1e-6,
}


class Agent(AbstractAgent):
    """SAC agent interacting with environment.

    Attrtibutes:
        memory (PrioritizedReplayBufferfD): replay memory
        actor (nn.Module): actor model to select actions
        actor_target (nn.Module): target actor model to select actions
        actor_optimizer (Optimizer): optimizer for training actor
        critic_1 (nn.Module): critic model to predict state values
        critic_2 (nn.Module): critic model to predict state values
        critic_target1 (nn.Module): target critic model to predict state values
        critic_target2 (nn.Module): target critic model to predict state values
        critic_optimizer1 (Optimizer): optimizer for training critic_1
        critic_optimizer2 (Optimizer): optimizer for training critic_2
        curr_state (np.ndarray): temporary storage of the current state
        n_step (int): iteration number of the current episode
        target_entropy (int): desired entropy used for the inequality constraint
        alpha (torch.Tensor): weight for entropy
        alpha_optimizer (Optimizer): optimizer for alpha

    """

    def __init__(self, env: gym.Env, args: argparse.Namespace):
        """Initialization.

        Args:
            env (gym.Env): openAI Gym environment with discrete action space
            args (argparse.Namespace): arguments including hyperparameters and training settings

        """
        AbstractAgent.__init__(self, env, args)

        self.curr_state = np.zeros((self.state_dim,))
        self.n_step = 0

        # create actor
        self.actor = Actor(self.state_dim, self.action_dim).to(device)

        # create v_critic
        self.vf = Value(self.state_dim).to(device)
        self.vf_target = Value(self.state_dim).to(device)
        self.vf_target.load_state_dict(self.vf.state_dict())

        # create q_critic
        self.qf_1 = Qvalue(self.state_dim, self.action_dim).to(device)
        self.qf_2 = Qvalue(self.state_dim, self.action_dim).to(device)

        # create optimizers
        self.actor_optimizer = optim.Adam(
            self.actor.parameters(), lr=hyper_params["LR_ACTOR"]
        )
        self.vf_optimizer = optim.Adam(self.vf.parameters(), lr=hyper_params["LR_VF"])
        self.qf_1_optimizer = optim.Adam(
            self.qf_1.parameters(), lr=hyper_params["LR_QF1"]
        )
        self.qf_2_optimizer = optim.Adam(
            self.qf_2.parameters(), lr=hyper_params["LR_QF2"]
        )

        # automatic entropy tuning
        if hyper_params["AUTO_ENTROPY_TUNING"]:
            self.target_entropy = -np.prod((self.action_dim,)).item()  # heuristic
            self.log_alpha = torch.zeros(1, requires_grad=True, device=device)
            self.alpha_optimizer = optim.Adam(
                [self.log_alpha], lr=hyper_params["LR_ENTROPY"]
            )

        # load the optimizer and model parameters
        if args.load_from is not None and os.path.exists(args.load_from):
            self.load_params(args.load_from)

        # load demo replay memory
        with open(self.args.demo_path, "rb") as f:
            demo = pickle.load(f)

        # replay memory
        self.beta = hyper_params["PER_BETA"]
        self.memory = PrioritizedReplayBufferfD(
            hyper_params["BUFFER_SIZE"],
            hyper_params["BATCH_SIZE"],
            self.args.seed,
            demo=demo,
            alpha=hyper_params["PER_ALPHA"],
        )

    def select_action(self, state: np.ndarray) -> torch.Tensor:
        """Select an action from the input space."""
        self.curr_state = state

        state = torch.FloatTensor(state).to(device)
        selected_action, _, _, _, _ = self.actor(state)

        return selected_action

    def step(self, action: torch.Tensor) -> Tuple[np.ndarray, np.float64, bool]:
        """Take an action and return the response of the env."""
        action = action.detach().cpu().numpy()
        next_state, reward, done, _ = self.env.step(action)

        self.memory.add(self.curr_state, action, reward, next_state, done)

        return next_state, reward, done

    def update_model(
        self, experiences: Tuple[torch.Tensor, ...]
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """Train the model after each episode."""
        states, actions, rewards, next_states, dones, weights, indexes, eps_d = (
            experiences
        )
        new_actions, log_prob, pre_tanh_value, mu, std = self.actor(states)

        # train alpha
        if hyper_params["AUTO_ENTROPY_TUNING"]:
            alpha_loss = torch.mean(
                (-self.log_alpha * (log_prob + self.target_entropy).detach()) * weights
            )

            self.alpha_optimizer.zero_grad()
            alpha_loss.backward()
            self.alpha_optimizer.step()

            alpha = self.log_alpha.exp()
        else:
            alpha_loss = 0.0
            alpha = hyper_params["W_ENTROPY"]

        # Q function loss
        masks = 1 - dones
        q_1_pred = self.qf_1(states, actions)
        q_2_pred = self.qf_2(states, actions)
        v_target = self.vf_target(next_states)
        q_target = rewards + hyper_params["GAMMA"] * v_target * masks
        qf_1_loss = torch.mean((q_1_pred - q_target.detach()).pow(2) * weights)
        qf_2_loss = torch.mean((q_2_pred - q_target.detach()).pow(2) * weights)

        # V function loss
        v_pred = self.vf(states)
        q_pred = torch.min(
            self.qf_1(states, new_actions), self.qf_2(states, new_actions)
        )
        v_target = (q_pred - alpha * log_prob).detach()
        vf_loss = torch.mean((v_pred - v_target).pow(2) * weights)

        # train Q functions
        self.qf_1_optimizer.zero_grad()
        qf_1_loss.backward()
        self.qf_1_optimizer.step()

        self.qf_2_optimizer.zero_grad()
        qf_2_loss.backward()
        self.qf_2_optimizer.step()

        # train V function
        self.vf_optimizer.zero_grad()
        vf_loss.backward()
        self.vf_optimizer.step()

        if self.n_step % hyper_params["DELAYED_UPDATE"] == 0:
            # actor loss
            advantage = q_pred - v_pred.detach()
            actor_loss_element_wise = alpha * log_prob - advantage
            actor_loss = torch.mean(actor_loss_element_wise * weights)

            # regularization
            mean_reg = hyper_params["W_MEAN_REG"] * mu.pow(2).mean()
            std_reg = hyper_params["W_STD_REG"] * std.pow(2).mean()
            pre_activation_reg = hyper_params["W_PRE_ACTIVATION_REG"] * (
                pre_tanh_value.pow(2).sum(dim=1).mean()
            )
            actor_reg = mean_reg + std_reg + pre_activation_reg

            # actor loss + regularization
            actor_loss += actor_reg

            # train actor
            self.actor_optimizer.zero_grad()
            actor_loss.backward()
            self.actor_optimizer.step()

            # update target networks
            common_utils.soft_update(self.vf, self.vf_target, hyper_params["TAU"])

            # update priorities
            new_priorities = (v_pred - v_target).pow(2)
            new_priorities += hyper_params["LAMDA3"] * actor_loss_element_wise.pow(2)
            new_priorities += hyper_params["PER_EPS"]
            new_priorities = new_priorities.data.cpu().numpy().squeeze()
            new_priorities += eps_d
            self.memory.update_priorities(indexes, new_priorities)
        else:
            actor_loss = torch.zeros(1)

        return (
            actor_loss.data,
            qf_1_loss.data,
            qf_2_loss.data,
            vf_loss.data,
            alpha_loss.data,
        )

    def load_params(self, path: str):
        """Load model and optimizer parameters."""
        if not os.path.exists(path):
            print("[ERROR] the input path does not exist. ->", path)
            return

        params = torch.load(path)
        self.actor.load_state_dict(params["actor"])
        self.qf_1.load_state_dict(params["qf_1"])
        self.qf_2.load_state_dict(params["qf_2"])
        self.vf.load_state_dict(params["vf"])
        self.vf_target.load_state_dict(params["vf_target"])
        self.actor_optimizer.load_state_dict(params["actor_optim"])
        self.qf_1_optimizer.load_state_dict(params["qf_1_optim"])
        self.qf_2_optimizer.load_state_dict(params["qf_2_optim"])
        self.vf_optimizer.load_state_dict(params["vf_optim"])

        print("[INFO] loaded the model and optimizer from", path)

    def save_params(self, n_episode: int):
        """Save model and optimizer parameters."""
        params = {
            "actor": self.actor.state_dict(),
            "qf_1": self.qf_1.state_dict(),
            "qf_2": self.qf_2.state_dict(),
            "vf": self.vf.state_dict(),
            "vf_target": self.vf_target.state_dict(),
            "actor_optim": self.actor_optimizer.state_dict(),
            "qf_1_optim": self.qf_1_optimizer.state_dict(),
            "qf_2_optim": self.qf_2_optimizer.state_dict(),
            "vf_optim": self.vf_optimizer.state_dict(),
        }

        AbstractAgent.save_params(self, self.args.algo, params, n_episode)

    def write_log(
        self,
        i: int,
        loss: np.ndarray,
        score: float = 0.0,
        delayed_update: int = 1,
        is_step: bool = False,
    ):
        """Write log about loss and score"""
        total_loss = loss.sum()

        message = "episode"
        if is_step:
            message = "step"

        print(
            "[INFO] " + message + " %d total score: %d, total loss: %f\n"
            "actor_loss: %.3f qf_1_loss: %.3f qf_2_loss: %.3f "
            "vf_loss: %.3f alpha_loss: %.3f\n"
            % (
                i,
                score,
                total_loss,
                loss[0] * delayed_update,  # actor loss
                loss[1],  # qf_1 loss
                loss[2],  # qf_2 loss
                loss[3],  # vf loss
                loss[4],  # alpha loss
            )
        )

        if self.args.log:
            wandb.log(
                {
                    "score": score,
                    "total loss": total_loss,
                    "actor loss": loss[0] * delayed_update,
                    "qf_1 loss": loss[1],
                    "qf_2 loss": loss[2],
                    "vf loss": loss[3],
                    "alpha loss": loss[4],
                }
            )

    def pretrain(self):
        """Pretraining steps."""
        self.n_step = 0
        pretrain_loss = list()
        print("[INFO] Pre-Train %d steps." % hyper_params["PRETRAIN_STEP"])
        for i_step in range(1, hyper_params["PRETRAIN_STEP"] + 1):
            experiences = self.memory.sample()
            loss = self.update_model(experiences)
            pretrain_loss.append(loss)  # for logging
            self.n_step += 1

            # logging
            if i_step == 1 or i_step % 100 == 0:
                avg_loss = np.vstack(pretrain_loss).mean(axis=0)
                pretrain_loss.clear()
                self.write_log(
                    i_step,
                    avg_loss,
                    delayed_update=hyper_params["DELAYED_UPDATE"],
                    is_step=True,
                )

    def train(self):
        """Train the agent."""
        # logger
        if self.args.log:
            wandb.init()
            wandb.config.update(hyper_params)
            wandb.watch([self.actor, self.vf, self.qf_1, self.qf_2], log="parameters")

        # pre-training by demo
        self.pretrain()

        # train
        print("[INFO] Train Start.")
        self.n_step = 0
        for i_episode in range(1, self.args.episode_num + 1):
            state = self.env.reset()
            done = False
            score = 0
            loss_episode = list()

            while not done:
                if self.args.render and i_episode >= self.args.render_after:
                    self.env.render()

                action = self.select_action(state)
                next_state, reward, done = self.step(action)

                if len(self.memory) >= hyper_params["BATCH_SIZE"]:
                    loss_multiple_learn = []
                    for _ in range(hyper_params["MULTIPLE_LEARN"]):
                        experiences = self.memory.sample(self.beta)
                        loss = self.update_model(experiences)
                        loss_multiple_learn.append(loss)
                    # for logging
                    loss_episode.append(np.vstack(loss_multiple_learn).mean(axis=0))

                # increase beta
                fraction = min(float(i_episode) / self.args.max_episode_steps, 1.0)
                self.beta = self.beta + fraction * (1.0 - self.beta)

                state = next_state
                score += reward
                self.n_step += 1

            # logging
            if loss_episode:
                avg_loss = np.vstack(loss_episode).mean(axis=0)
                self.write_log(
                    i_episode, avg_loss, score, hyper_params["DELAYED_UPDATE"]
                )

            if i_episode % self.args.save_period == 0:
                self.save_params(i_episode)

        # termination
        self.env.close()