from collections import deque

import numpy as np
from gym.spaces import Box, Discrete


class LazyFrames:
    """
    Stacked frames which never allocate memory to the same frame.
    """

    def __init__(self, frames):
        self._frames = list(frames)

    def __array__(self, dtype):
        return np.array(self._frames, dtype=dtype)

    def __len__(self):
        return len(self._frames)


class SequenceBuffer:
    """
    Buffer for storing sequence data.
    """

    def __init__(self, num_sequences=8):
        self.num_sequences = num_sequences
        self.reset()

    def reset(self):
        self._reset_episode = False
        self.state_ = deque(maxlen=self.num_sequences + 1)
        self.action_ = deque(maxlen=self.num_sequences)
        self.reward_ = deque(maxlen=self.num_sequences)

    def reset_episode(self, state):
        assert not self._reset_episode
        self._reset_episode = True
        self.state_.append(state)

    def append(self, action, reward, next_state):
        assert self._reset_episode
        self.action_.append(action)
        self.reward_.append([reward])
        self.state_.append(next_state)

    def get(self):
        state_ = LazyFrames(self.state_)
        action_ = np.array(self.action_, dtype=np.float32)
        reward_ = np.array(self.reward_, dtype=np.float32)
        return state_, action_, reward_

    def is_empty(self):
        return len(self.reward_) == 0

    def is_full(self):
        return len(self.reward_) == self.num_sequences

    def __len__(self):
        return len(self.reward_)


class SLACReplayBuffer:
    """
    Replay Buffer for SLAC.
    """

    def __init__(
        self,
        buffer_size,
        state_space,
        action_space,
        num_sequences,
    ):
        assert len(state_space.shape) in (1, 3)

        self._n = 0
        self._p = 0
        self.buffer_size = buffer_size
        self.num_sequences = num_sequences
        self.state_shape = state_space.shape
        self.use_image = len(self.state_shape) == 3

        if self.use_image:
            # Store images as a list of LazyFrames, which uses 4 times less memory.
            self.state_ = [None] * buffer_size
        else:
            self.state_ = np.empty((buffer_size, num_sequences + 1, *state_space.shape), dtype=np.float32)

        if type(action_space) == Box:
            self.action_ = np.empty((buffer_size, num_sequences, *action_space.shape), dtype=np.float32)
        elif type(action_space) == Discrete:
            self.action_ = np.empty((buffer_size, num_sequences, 1), dtype=np.int32)
        else:
            NotImplementedError

        self.reward_ = np.empty((buffer_size, num_sequences, 1), dtype=np.float32)
        self.done = np.empty((buffer_size, 1), dtype=np.float32)

        # Buffer to store a sequence of trajectories.
        self.seq_buffer = SequenceBuffer(num_sequences)

    def reset_episode(self, state):
        """
        Reset the sequence buffer and set the initial observation. This has to be done before every episode starts.
        """
        self.seq_buffer.reset_episode(state)

    def append(self, action, reward, done, next_state, episode_done=None):
        self.seq_buffer.append(action, reward, next_state)

        if self.seq_buffer.is_full():
            state_, action_, reward_ = self.seq_buffer.get()
            self._append(state_, action_, reward_, done)

        if episode_done:
            self.seq_buffer.reset()

    def _append(self, state_, action_, reward_, done):
        self.state_[self._p] = state_
        self.action_[self._p] = action_
        self.reward_[self._p] = reward_
        self.done[self._p] = float(done)

        self._p = (self._p + 1) % self.buffer_size
        self._n = min(self._n + 1, self.buffer_size)

    def _sample_idx(self, batch_size):
        return np.random.randint(low=0, high=self._n, size=batch_size)

    def _sample_state(self, idxes):
        if self.use_image:
            state_ = np.empty((len(idxes), self.num_sequences + 1, *self.state_shape), dtype=np.uint8)
            for i, idx in enumerate(idxes):
                state_[i, ...] = self.state_[idx]
        else:
            state_ = self.state_[idxes]
        return state_

    def sample_latent(self, batch_size):
        idxes = self._sample_idx(batch_size)
        return (self._sample_state(idxes), self.action_[idxes], self.reward_[idxes], self.done[idxes])

    def sample_sac(self, batch_size):
        idxes = self._sample_idx(batch_size)
        return (self._sample_state(idxes), self.action_[idxes], self.reward_[idxes, -1], self.done[idxes])
