#!/usr/bin/env python3
"""Deep Q-Network (DQN) for comment tone and subreddit choice.

Same public API as the old tabular agent so the joiner does not change.
All public methods swallow errors so a bad pickle or DB blip cannot stop a profile.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import pickle
import random
import signal
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np

from reddit_joiner.paths import DB_PATH as _DB_PATH, RL_MODEL_FILE as _RL_MODEL_FILE

DEFAULT_MODEL = str(_RL_MODEL_FILE)
DEFAULT_DB = str(_DB_PATH)

COMMENT_ACTIONS = (
    "skip",
    "comment:friendly",
    "comment:expert",
    "comment:funny",
    "comment:neutral",
)
COMMENT_TONE_ACTIONS = (
    "comment:friendly",
    "comment:expert",
    "comment:funny",
    "comment:neutral",
)
JOIN_ACTIONS = (
    "join:comment",
    "join:lurk",
)

ARCH_VERSION = 4
# Rewards are clipped before training. Raw values span -20..+15, and a single
# -20 drowns out dozens of ordinary outcomes in an MSE gradient.
REWARD_CLIP = 10.0
# Largest allowed parameter update per step, so one odd batch cannot wreck the net.
GRAD_CLIP = 1.0
# L2 pull toward zero. This model sees a few hundred graded outcomes at most, so
# it needs real regularisation to avoid memorising them.
WEIGHT_DECAY = 1e-4
# Shrinkage strength for the count-based prior: with fewer than this many
# observations for a (context, action) pair, the net's guess still dominates.
PRIOR_STRENGTH = 4.0
# Exploration bonus for rarely-tried actions (UCB). Scaled in reward units.
UCB_BONUS = 1.5
# How much past experience to carry between runs, so learning is not restarted.
REPLAY_PERSIST = 1500
# Gradient steps taken per new observation. Scarce data is worth revisiting.
TRAIN_ITERS = 4
# Gradient steps run once at startup against everything already known.
WARM_START_STEPS = 120
STATE_NUM = 20
HASH_DIM = 8
ACTION_DIM = 16
STATE_DIM = STATE_NUM + HASH_DIM * 3
INPUT_DIM = STATE_DIM + ACTION_DIM
# Deliberately small. The old (64, 32) net carried ~4,900 weights against a few
# hundred observations, which is far more capacity than the data can support.
HIDDEN = (24, 12)


def karma_bucket(karma: int) -> str:
    value = max(0, int(karma or 0))
    if value < 50:
        return "k0"
    if value < 200:
        return "k50"
    if value < 500:
        return "k200"
    return "k500"


def age_bucket(age_days: float) -> str:
    value = max(0.0, float(age_days or 0))
    if value < 8:
        return "a0"
    if value < 31:
        return "a8"
    if value < 91:
        return "a31"
    return "a90"


def length_bucket(length: int) -> str:
    value = max(0, int(length or 0))
    if value < 80:
        return "l0"
    if value < 300:
        return "l80"
    if value < 1000:
        return "l300"
    return "l1000"


def sentiment_bucket(score: float) -> str:
    try:
        tenths = int(round(float(score) * 10))
    except (TypeError, ValueError):
        tenths = 0
    tenths = max(-10, min(10, tenths))
    return f"s{tenths}"


def build_state(
    *,
    account_id: str = "",
    account_karma: int = 0,
    account_age_days: float = 0.0,
    target_subreddit: str = "",
    post_sentiment: float = 0.0,
    post_length: int = 0,
    kind: str = "",
    session_comments: int = 0,
    success_rate: float = 0.0,
    comment_length: int = 0,
    rules_count: float = 0.0,
    rules_strict: float = 0.0,
    rules_flags: float = 0.0,
    rules_read: float = 0.0,
    rules_fp: str = "",
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    stamp = now or datetime.now()
    return {
        "account_id": str(account_id or ""),
        "account_karma": int(account_karma or 0),
        "account_age_days": float(account_age_days or 0),
        "target_subreddit": str(target_subreddit or "").strip().lstrip("r/").lower(),
        "post_sentiment": float(post_sentiment or 0),
        "post_length": int(post_length or 0),
        "comment_length": int(comment_length or 0),
        "time_of_day": stamp.hour,
        "day_of_week": stamp.weekday(),
        "kind": str(kind or "").strip().lower(),
        "session_comments": int(session_comments or 0),
        "success_rate": float(success_rate or 0),
        "rules_count": float(rules_count or 0),
        "rules_strict": float(rules_strict or 0),
        "rules_flags": float(rules_flags or 0),
        "rules_read": float(rules_read or 0),
        "rules_fp": str(rules_fp or ""),
    }


def state_key(state: Dict[str, Any]) -> str:
    # account_id is deliberately absent: with dozens of accounts it splits the
    # data into one-sample states and nothing generalizes. Karma and age tiers
    # carry what actually matters about an account.
    return "|".join(
        [
            karma_bucket(int(state.get("account_karma") or 0)),
            age_bucket(float(state.get("account_age_days") or 0)),
            str(state.get("target_subreddit") or "none")[:40],
            sentiment_bucket(float(state.get("post_sentiment") or 0)),
            length_bucket(int(state.get("post_length") or 0)),
            f"h{int(state.get('time_of_day') or 0) // 6}",
            f"d{int(state.get('day_of_week') or 0)}",
            str(state.get("kind") or "any")[:10],
            length_bucket(int(state.get("comment_length") or 0)),
            f"rr{int(float(state.get('rules_read') or 0))}",
            f"rs{int(min(max(float(state.get('rules_strict') or 0), 0.0), 1.0) * 10)}",
            str(state.get("rules_fp") or "")[:24],
        ]
    )


def context_key(state: Any) -> str:
    """
    Coarse bucket for the count-based prior.

    Much blunter than state_key on purpose: it has to collect enough
    observations per bucket to mean something, so it keeps only the handful of
    things that plausibly drive an outcome — how established the account is,
    which community, and what kind of action this was.
    """
    if not isinstance(state, dict):
        return str(state or "")[:48]
    return "|".join(
        [
            karma_bucket(int(state.get("account_karma") or 0)),
            str(state.get("target_subreddit") or "none")[:32],
            str(state.get("kind") or "any")[:10],
        ]
    )


def _hash_vec(text: str, dim: int) -> np.ndarray:
    vec = np.zeros(dim, dtype=np.float32)
    raw = (text or "").encode("utf-8", errors="ignore")
    if not raw:
        return vec
    digest = hashlib.sha256(raw).digest()
    for index in range(dim):
        vec[index] = digest[index % len(digest)] / 127.5 - 1.0
    return vec


def encode_state(state: Any) -> np.ndarray:
    if not isinstance(state, dict):
        key = str(state or "")
        numeric = np.zeros(STATE_NUM, dtype=np.float32)
        return np.concatenate([numeric, _hash_vec(key, HASH_DIM), _hash_vec("key", HASH_DIM)])
    karma = max(0.0, float(state.get("account_karma") or 0))
    age = max(0.0, float(state.get("account_age_days") or 0))
    length = max(0.0, float(state.get("post_length") or 0))
    sentiment = max(-1.0, min(1.0, float(state.get("post_sentiment") or 0)))
    hour = max(0.0, min(23.0, float(state.get("time_of_day") or 0)))
    weekday = max(0.0, min(6.0, float(state.get("day_of_week") or 0)))
    kind = str(state.get("kind") or "").strip().lower()
    comment_len = max(0.0, float(state.get("comment_length") or 0))
    hour_rad = 2.0 * np.pi * (hour / 24.0)
    day_rad = 2.0 * np.pi * (weekday / 7.0)
    numeric = np.array(
        [
            np.log1p(karma) / 8.0,
            min(age / 365.0, 2.0),
            sentiment,
            np.log1p(length) / 8.0,
            hour / 23.0,
            weekday / 6.0,
            float(np.sin(hour_rad)),
            float(np.cos(hour_rad)),
            float(np.sin(day_rad)),
            float(np.cos(day_rad)),
            1.0 if kind == "sheet" else (0.5 if kind == "post" else 0.0),
            1.0 if kind in {"browse", "karma", "subreddit"} else 0.0,
            min(max(float(state.get("session_comments") or 0) / 8.0, 0.0), 1.0),
            # Slot kept for layout compatibility. The old global success_rate
            # feature was non-stationary — every row shifted it — so the same
            # situation encoded differently over time and the net chased noise.
            0.0,
            1.0 if weekday >= 5.0 else 0.0,
            np.log1p(comment_len) / 8.0,
            min(max(float(state.get("rules_count") or 0), 0.0), 1.0),
            min(max(float(state.get("rules_strict") or 0), 0.0), 1.0),
            min(max(float(state.get("rules_flags") or 0), 0.0), 1.0),
            1.0 if float(state.get("rules_read") or 0) > 0 else 0.0,
        ],
        dtype=np.float32,
    )
    return np.concatenate(
        [
            numeric,
            _hash_vec(str(state.get("target_subreddit") or ""), HASH_DIM),
            # Account identity is intentionally not encoded — it would let the
            # net memorise per-account quirks from a couple of samples instead
            # of learning what works on Reddit generally.
            np.zeros(HASH_DIM, dtype=np.float32),
            _hash_vec(str(state.get("rules_fp") or ""), HASH_DIM),
        ]
    )


def encode_action(action: str) -> np.ndarray:
    return _hash_vec(str(action or "").strip().lower(), ACTION_DIM)


def encode_pair(state: Any, action: str) -> np.ndarray:
    return np.concatenate([encode_state(state), encode_action(action)])


def reward_from_status(
    info: Optional[Dict[str, Any]],
    *,
    still_live: float = 1.0,
    score_2: float = 2.0,
    upvote_5: float = 5.0,
    upvote_20: float = 10.0,
    score_neg: float = -2.0,
    downvote: float = -5.0,
    removed: float = -8.0,
    filtered: float = -3.0,
) -> Optional[float]:
    """Map a live/removed/score check to a DQN reward. None means try again later."""
    if not info:
        return None
    status = str(info.get("status") or "").strip().lower()
    try:
        score = int(info.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    if status in {"removed", "deleted"}:
        return float(removed)
    if status in {"filtered", "collapsed"}:
        return float(filtered)
    if status != "live":
        return None
    if score >= 20:
        return float(upvote_20)
    if score >= 5:
        return float(upvote_5)
    if score >= 2:
        return float(score_2)
    if score <= -3:
        return float(downvote)
    if score < 0:
        return float(score_neg)
    return float(still_live)


class _QNet:
    """Two-hidden-layer MLP that scores Q(s, a)."""

    def __init__(self, rng: np.random.Generator) -> None:
        self.W1 = self._xavier(rng, INPUT_DIM, HIDDEN[0])
        self.b1 = np.zeros(HIDDEN[0], dtype=np.float32)
        self.W2 = self._xavier(rng, HIDDEN[0], HIDDEN[1])
        self.b2 = np.zeros(HIDDEN[1], dtype=np.float32)
        self.W3 = self._xavier(rng, HIDDEN[1], 1)
        self.b3 = np.zeros(1, dtype=np.float32)

    @staticmethod
    def _xavier(rng: np.random.Generator, rows: int, cols: int) -> np.ndarray:
        scale = np.sqrt(2.0 / max(1, rows + cols))
        return rng.normal(0.0, scale, size=(rows, cols)).astype(np.float32)

    def copy_from(self, other: "_QNet") -> None:
        self.W1 = other.W1.copy()
        self.b1 = other.b1.copy()
        self.W2 = other.W2.copy()
        self.b2 = other.b2.copy()
        self.W3 = other.W3.copy()
        self.b3 = other.b3.copy()

    def weights(self) -> Dict[str, np.ndarray]:
        return {
            "W1": self.W1,
            "b1": self.b1,
            "W2": self.W2,
            "b2": self.b2,
            "W3": self.W3,
            "b3": self.b3,
        }

    def load_weights(self, payload: Dict[str, Any]) -> bool:
        try:
            for name in ("W1", "b1", "W2", "b2", "W3", "b3"):
                value = np.asarray(payload[name], dtype=np.float32)
                if value.shape != getattr(self, name).shape:
                    return False
                setattr(self, name, value)
            return True
        except Exception:
            return False

    def forward(self, batch: np.ndarray) -> Tuple[np.ndarray, Dict[str, np.ndarray]]:
        x = np.asarray(batch, dtype=np.float32)
        if x.ndim == 1:
            x = x.reshape(1, -1)
        z1 = x @ self.W1 + self.b1
        h1 = np.maximum(z1, 0.0)
        z2 = h1 @ self.W2 + self.b2
        h2 = np.maximum(z2, 0.0)
        q = h2 @ self.W3 + self.b3
        return q.reshape(-1), {"x": x, "z1": z1, "h1": h1, "z2": z2, "h2": h2}

    def predict(self, batch: np.ndarray) -> np.ndarray:
        q, _ = self.forward(batch)
        return q

    def train_batch(self, batch: np.ndarray, targets: np.ndarray, lr: float) -> float:
        q, cache = self.forward(batch)
        y = np.asarray(targets, dtype=np.float32).reshape(-1)
        error = q - y
        loss = float(np.mean(error ** 2))
        dout = (2.0 * error / max(1, error.size)).reshape(-1, 1)
        dW3 = cache["h2"].T @ dout
        db3 = dout.sum(axis=0)
        dh2 = dout @ self.W3.T
        dz2 = dh2 * (cache["z2"] > 0)
        dW2 = cache["h1"].T @ dz2
        db2 = dz2.sum(axis=0)
        dh1 = dz2 @ self.W2.T
        dz1 = dh1 * (cache["z1"] > 0)
        dW1 = cache["x"].T @ dz1
        db1 = dz1.sum(axis=0)
        grads = [dW3, db3, dW2, db2, dW1, db1]
        norm = float(np.sqrt(sum(float(np.sum(g * g)) for g in grads)))
        scale = GRAD_CLIP / norm if norm > GRAD_CLIP else 1.0
        dW3, db3, dW2, db2, dW1, db1 = (g * scale for g in grads)
        dW3 = dW3 + WEIGHT_DECAY * self.W3
        dW2 = dW2 + WEIGHT_DECAY * self.W2
        dW1 = dW1 + WEIGHT_DECAY * self.W1
        self.W3 -= lr * dW3.astype(np.float32)
        self.b3 -= lr * db3.astype(np.float32)
        self.W2 -= lr * dW2.astype(np.float32)
        self.b2 -= lr * db2.astype(np.float32)
        self.W1 -= lr * dW1.astype(np.float32)
        self.b1 -= lr * db1.astype(np.float32)
        return loss


class RLAgent:
    """
    One learner: a small Q-network whose estimate is shrunk toward the measured
    average for each (context, action) bucket, with UCB exploration.

    There is no second tabular agent. An earlier tabular version was replaced by
    this network and left its `alpha` and `q_table` behind; both are gone, since
    keeping two half-wired estimators around only invites confusion about which
    one decides anything.

    Every outcome here is terminal — the score a comment earns does not depend
    on the next comment — so this is a contextual bandit, not sequential RL.
    `gamma` and the target network were therefore never reachable (no caller has
    ever passed a `next_state`) and have been removed rather than left inert.
    """

    def __init__(
        self,
        *,
        model_file: str = DEFAULT_MODEL,
        db_path: str = DEFAULT_DB,
        epsilon: float = 0.30,
        # With only a few hundred graded outcomes there is not enough evidence to
        # commit to a tone, so keep a real exploration floor.
        epsilon_min: float = 0.30,
        epsilon_decay: float = 0.995,
        alpha: float = 0.001,
        gamma: float = 0.9,
        save_interval: int = 10,
        replay_size: int = 3000,
        batch_size: int = 16,
        target_sync: int = 20,
        dqn_lr: float = 0.001,
    ) -> None:
        self.kind = "bandit-qnet"
        self.model_file = model_file
        self.db_path = db_path
        self.epsilon_init = float(epsilon)
        self.epsilon = float(epsilon)
        self.epsilon_min = float(epsilon_min)
        self.epsilon_decay = float(epsilon_decay)
        # `alpha`, `gamma` and `target_sync` are accepted so existing callers and
        # config files keep working, but they no longer steer anything: the step
        # size is dqn_lr, and there is no bootstrapping to discount.
        self.lr = max(1e-5, float(dqn_lr or 0.001))
        self.save_interval = max(1, int(save_interval))
        self.batch_size = max(4, int(batch_size))
        self.experience_buffer: List[Dict[str, Any]] = []
        self.replay: deque = deque(maxlen=max(64, int(replay_size)))
        # (context, action) -> {"n": observations, "mean": average reward}
        self.action_stats: Dict[str, Dict[str, Any]] = {}
        self.action_count = 0
        self.train_steps = 0
        self.total_reward = 0.0
        self.total_success = 0
        self.total_fail = 0
        self.last_loss = 0.0
        self.graded_actions = 0
        self.graded_reward = 0.0
        self.unchecked_retired = 0
        self.last_status_updates: List[Dict[str, Any]] = []
        self._handlers_installed = False
        self._lock = threading.RLock()
        self._rng = np.random.default_rng()
        self.online = _QNet(self._rng)
        self.loaded_weights = False
        self.arch_reset = False
        self.load_model()
        self._ensure_db()
        try:
            self.warm_start()
        except Exception:
            pass

    def _clip_reward(self, reward: float) -> float:
        """Keep one bad outcome from dominating the whole gradient."""
        try:
            value = float(reward)
        except (TypeError, ValueError):
            return 0.0
        if value != value:  # NaN
            return 0.0
        return max(-REWARD_CLIP, min(REWARD_CLIP, value))

    def get_state_key(self, state: Dict[str, Any]) -> str:
        return state_key(state or {})

    def get_q_value(self, state: Any, action: str) -> float:
        """Shrunk estimate: what we measured here, backed off to the net's guess."""
        try:
            net = float(self.online.predict(encode_pair(state, action))[0])
        except Exception:
            net = 0.0
        count, mean = self._prior_for(state, action)
        if count <= 0:
            return net
        # Empirical-Bayes shrinkage. Few observations -> trust the net; many
        # observations -> trust what actually happened in this bucket.
        weight = float(count) / (float(count) + PRIOR_STRENGTH)
        return weight * mean + (1.0 - weight) * net

    def _prior_key(self, state: Any, action: str) -> str:
        return f"{context_key(state)}||{str(action)}"

    def _prior_for(self, state: Any, action: str) -> Tuple[int, float]:
        row = self.action_stats.get(self._prior_key(state, action))
        if not isinstance(row, dict):
            return 0, 0.0
        return int(row.get("n") or 0), float(row.get("mean") or 0.0)

    def _record_prior(self, state: Any, action: str, reward: float) -> None:
        key = self._prior_key(state, action)
        row = self.action_stats.get(key)
        if not isinstance(row, dict):
            row = {"n": 0, "mean": 0.0}
        count = int(row.get("n") or 0) + 1
        mean = float(row.get("mean") or 0.0)
        # Running mean, so a bucket never needs its history kept around.
        row["n"] = count
        row["mean"] = mean + (float(reward) - mean) / float(count)
        self.action_stats[key] = row
        if len(self.action_stats) > 4000:
            for stale in list(self.action_stats.keys())[:500]:
                self.action_stats.pop(stale, None)

    def _exploration_bonus(self, state: Any, action: str) -> float:
        """UCB: prefer actions this context has barely tried."""
        count, _ = self._prior_for(state, action)
        total = max(1, self.graded_actions)
        return UCB_BONUS * float(np.sqrt(np.log(total + 1.0) / (count + 1.0)))

    def _sample_batch(self) -> List[Dict[str, Any]]:
        """
        Draw a training minibatch, biased toward real graded outcomes.

        Sampling is done WITH replacement so a thin buffer still trains. The old
        code required batch_size rows before taking a single step, and since the
        buffer began every run empty and a run produces only a handful of
        actions, it never trained at all.
        """
        rows = list(self.replay)
        if not rows:
            return []
        graded = [row for row in rows if row.get("graded")]
        picks: List[Dict[str, Any]] = []
        for _ in range(self.batch_size):
            pool = graded if (graded and random.random() < 0.75) else rows
            picks.append(random.choice(pool))
        return picks

    def _train_replay(self, iters: int = TRAIN_ITERS) -> None:
        if not self.replay:
            return
        for _ in range(max(1, int(iters))):
            sample = self._sample_batch()
            if not sample:
                return
            inputs = np.stack([encode_pair(row["state"], row["action"]) for row in sample])
            # Outcomes are terminal, so the regression target is the observed
            # reward itself — no bootstrapping from a next state.
            targets = np.array([float(row["reward"]) for row in sample], dtype=np.float32)
            self.last_loss = self.online.train_batch(inputs, targets, self.lr)
            self.train_steps += 1

    def warm_start(self, steps: int = WARM_START_STEPS) -> int:
        """
        Train against everything already known before acting.

        Without this, a short run collects a few observations, never reaches a
        trainable batch, and throws them away — so the model never improves no
        matter how many runs happen.
        """
        with self._lock:
            self._load_pending_experience()
            if not self.replay:
                return 0
            before = self.train_steps
            self._train_replay(iters=steps)
            return self.train_steps - before

    def _load_pending_experience(self) -> int:
        """
        Recover graded outcomes from the database into the replay buffer.

        Rows recorded as removed with a score of exactly 0 are skipped: that is
        the signature of the old bug where a blocked HTTP read was written down
        as a removal, and replaying them would teach the fresh network the same
        false lesson that every comment gets deleted.
        """
        have = {
            f"{self.get_state_key(row['state'])}||{row['action']}||{row['reward']}"
            for row in self.replay
        }
        added = 0
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT action, reward, state_json
                    FROM rl_pending
                    WHERE applied = 1 AND reward IS NOT NULL AND state_json != ''
                      AND NOT (status = 'removed' AND score = 0)
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (int(REPLAY_PERSIST),),
                ).fetchall()
        except Exception:
            return 0
        for row in rows:
            try:
                state = json.loads(str(row["state_json"] or ""))
                if not isinstance(state, dict):
                    continue
                reward = self._clip_reward(row["reward"])
                action = str(row["action"] or "")
                if not action:
                    continue
                key = f"{self.get_state_key(state)}||{action}||{reward}"
                if key in have:
                    continue
                have.add(key)
                self.replay.append(
                    {
                        "state": state,
                        "action": action,
                        "reward": reward,
                        "next_state": None,
                        "next_actions": [],
                        "graded": True,
                    }
                )
                self._record_prior(state, action, reward)
                added += 1
            except Exception:
                continue
        return added

    def update_q_value(
        self,
        state: Any,
        action: str,
        reward: float,
        next_state: Any = None,
        next_actions: Optional[Iterable[str]] = None,
        graded: bool = False,
    ) -> None:
        """
        Train on one transition. `graded` marks a reward that came from a real
        observed outcome (a live/removed/score check) rather than bookkeeping
        like "rules were read". Only graded rows move epsilon and the quality
        metrics, so cheap guaranteed rewards cannot flatter the report.
        """
        with self._lock:
            try:
                reward = self._clip_reward(reward)
                actions = [str(item) for item in (next_actions or []) if str(item).strip()]
                self.replay.append(
                    {
                        "state": state,
                        "action": str(action),
                        "reward": float(reward),
                        "next_state": next_state,
                        "next_actions": actions,
                        "graded": bool(graded),
                    }
                )
                if graded:
                    self._record_prior(state, action, reward)
                self._train_replay()
                self.action_count += 1
                self.total_reward += float(reward)
                if float(reward) > 0:
                    self.total_success += 1
                elif float(reward) < 0:
                    self.total_fail += 1
                if graded:
                    self.graded_actions += 1
                    self.graded_reward += float(reward)
                self.experience_buffer.append(
                    {
                        "state_key": self.get_state_key(state) if isinstance(state, dict) else str(state),
                        "action": str(action),
                        "reward": float(reward),
                        "next_state_key": (
                            self.get_state_key(next_state)
                            if isinstance(next_state, dict)
                            else (str(next_state) if next_state is not None else "")
                        ),
                        "timestamp": datetime.now().isoformat(timespec="seconds"),
                        "success": 1 if float(reward) > 0 else 0,
                        "graded": 1 if graded else 0,
                    }
                )
                # Exploration should shrink as real outcomes arrive, not as
                # guaranteed bookkeeping rewards pile up.
                if graded:
                    self.decay_epsilon()
                if self.action_count % self.save_interval == 0:
                    self.save_model()
                    self.save_experience_to_db()
                    self.log_performance()
            except Exception:
                return

    def choose_action(self, state: Any, possible_actions: Iterable[str]) -> Optional[str]:
        actions = [str(item) for item in possible_actions if str(item).strip()]
        if not actions:
            return None
        with self._lock:
            try:
                if random.random() < self.epsilon:
                    return random.choice(actions)
                # Value plus an exploration bonus, so under-tried actions get
                # deliberately tried instead of waiting on a random epsilon roll.
                scores = [
                    self.get_q_value(state, action)
                    + self._exploration_bonus(state, action)
                    for action in actions
                ]
                best = max(scores)
                # Float equality almost never ties for a network, which used to
                # make the tie-break dead code. Treat near-equal as tied.
                tied = [
                    action
                    for action, score in zip(actions, scores)
                    if score >= best - 1e-3
                ]
                return random.choice(tied)
            except Exception:
                return random.choice(actions)

    def action_breakdown(self, top: int = 12) -> List[Dict[str, Any]]:
        """What the model has actually measured, best-first. For diagnostics."""
        rows: Dict[str, Dict[str, Any]] = {}
        for key, row in self.action_stats.items():
            action = key.split("||")[-1]
            item = rows.setdefault(action, {"action": action, "n": 0, "total": 0.0})
            count = int(row.get("n") or 0)
            item["n"] += count
            item["total"] += float(row.get("mean") or 0.0) * count
        out = []
        for item in rows.values():
            count = max(1, int(item["n"]))
            out.append(
                {"action": item["action"], "n": int(item["n"]), "mean": item["total"] / count}
            )
        out.sort(key=lambda row: row["mean"], reverse=True)
        return out[: max(1, int(top))]

    def decay_epsilon(self) -> None:
        try:
            self.epsilon = max(self.epsilon_min, self.epsilon * self.epsilon_decay)
        except Exception:
            return

    def save_model(self) -> None:
        try:
            payload = {
                "kind": self.kind,
                "arch": ARCH_VERSION,
                "input_dim": INPUT_DIM,
                "weights": self.online.weights(),
                "epsilon": self.epsilon,
                "action_count": self.action_count,
                "train_steps": self.train_steps,
                "total_reward": self.total_reward,
                "total_success": self.total_success,
                "total_fail": self.total_fail,
                "graded_actions": self.graded_actions,
                "graded_reward": self.graded_reward,
                "unchecked_retired": self.unchecked_retired,
                "action_stats": self.action_stats,
                # Carry experience forward. Weights alone are not enough when
                # each run only produces a handful of observations.
                "replay": list(self.replay)[-int(REPLAY_PERSIST) :],
                "saved_at": time.time(),
            }
            tmp = self.model_file + ".tmp"
            with open(tmp, "wb") as handle:
                pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, self.model_file)
        except Exception:
            return

    def load_model(self) -> None:
        if not os.path.isfile(self.model_file):
            return
        try:
            with open(self.model_file, "rb") as handle:
                payload = pickle.load(handle)
            if not isinstance(payload, dict):
                return
            same_arch = (
                int(payload.get("arch") or 0) == ARCH_VERSION
                and int(payload.get("input_dim") or 0) == INPUT_DIM
            )
            if isinstance(payload.get("weights"), dict) and same_arch:
                if self.online.load_weights(payload["weights"]):
                    self.loaded_weights = True
                    saved = float(payload.get("epsilon", self.epsilon))
                    self.epsilon = max(
                        self.epsilon_min,
                        min(saved, float(self.epsilon_init)),
                    )
                    self.action_count = int(payload.get("action_count") or 0)
                    self.train_steps = int(payload.get("train_steps") or 0)
                    self.total_reward = float(payload.get("total_reward") or 0)
                    self.total_success = int(payload.get("total_success") or 0)
                    self.total_fail = int(payload.get("total_fail") or 0)
                    self.graded_actions = int(payload.get("graded_actions") or 0)
                    self.graded_reward = float(payload.get("graded_reward") or 0)
                    self.unchecked_retired = int(payload.get("unchecked_retired") or 0)
                    stats = payload.get("action_stats")
                    if isinstance(stats, dict):
                        self.action_stats = {
                            str(key): dict(value)
                            for key, value in stats.items()
                            if isinstance(value, dict)
                        }
                    saved_replay = payload.get("replay")
                    if isinstance(saved_replay, list):
                        for row in saved_replay:
                            if isinstance(row, dict) and row.get("action"):
                                self.replay.append(row)
                    return
            self.arch_reset = True
        except Exception:
            return

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=60)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_db(self) -> None:
        try:
            with self._connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS rl_experience (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        state_key TEXT,
                        action TEXT,
                        reward REAL,
                        next_state_key TEXT,
                        timestamp TEXT,
                        account_id TEXT,
                        subreddit TEXT,
                        success INTEGER
                    );
                    CREATE TABLE IF NOT EXISTS rl_stats (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT,
                        total_actions INTEGER,
                        total_success INTEGER,
                        total_fail INTEGER,
                        average_reward REAL,
                        epsilon REAL
                    );
                    CREATE TABLE IF NOT EXISTS rl_pending (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        state_key TEXT,
                        action TEXT,
                        url TEXT,
                        kind TEXT,
                        created_at TEXT,
                        applied INTEGER DEFAULT 0
                    );
                    """
                )
                for column, ddl in (
                    ("status", "TEXT"),
                    ("score", "INTEGER"),
                    ("reward", "REAL"),
                    ("state_json", "TEXT"),
                ):
                    try:
                        conn.execute(f"ALTER TABLE rl_pending ADD COLUMN {column} {ddl}")
                    except Exception:
                        pass
                try:
                    conn.execute("ALTER TABLE rl_experience ADD COLUMN graded INTEGER DEFAULT 0")
                except Exception:
                    pass
        except Exception:
            return

    def save_experience_to_db(self) -> None:
        if not self.experience_buffer:
            return
        rows = list(self.experience_buffer)
        self.experience_buffer = []
        try:
            self._ensure_db()
            with self._connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO rl_experience
                    (state_key, action, reward, next_state_key, timestamp, account_id, subreddit, success, graded)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            row.get("state_key"),
                            row.get("action"),
                            row.get("reward"),
                            row.get("next_state_key"),
                            row.get("timestamp"),
                            row.get("account_id") or "",
                            row.get("subreddit") or "",
                            int(row.get("success") or 0),
                            int(row.get("graded") or 0),
                        )
                        for row in rows
                    ],
                )
        except Exception:
            self.experience_buffer = rows + self.experience_buffer

    def log_performance(self) -> None:
        try:
            self._ensure_db()
            avg = (self.total_reward / self.action_count) if self.action_count else 0.0
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO rl_stats
                    (timestamp, total_actions, total_success, total_fail, average_reward, epsilon)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        datetime.now().isoformat(timespec="seconds"),
                        self.action_count,
                        self.total_success,
                        self.total_fail,
                        avg,
                        self.epsilon,
                    ),
                )
        except Exception:
            return

    def queue_delayed(self, state: Any, action: str, url: str, kind: str) -> None:
        if not url:
            return
        try:
            self._ensure_db()
            state_json = ""
            if isinstance(state, dict):
                try:
                    state_json = json.dumps(state, default=str)
                except Exception:
                    state_json = ""
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO rl_pending
                    (state_key, action, url, kind, created_at, applied, state_json)
                    VALUES (?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        self.get_state_key(state) if isinstance(state, dict) else str(state),
                        str(action),
                        str(url),
                        str(kind),
                        datetime.now().isoformat(timespec="seconds"),
                        state_json,
                    ),
                )
        except Exception:
            return

    def pending_urls(self) -> set:
        try:
            self._ensure_db()
            with self._connect() as conn:
                rows = conn.execute("SELECT url FROM rl_pending").fetchall()
            return {str(row["url"] or "").split("?")[0].rstrip("/").lower() for row in rows}
        except Exception:
            return set()

    def apply_delayed_rewards(
        self,
        inspect_fn: Callable[[str], Any],
        *,
        upvote_5: float = 5,
        upvote_20: float = 10,
        downvote: float = -5,
        still_live: float = 1.0,
        score_2: float = 2.0,
        score_neg: float = -2.0,
        removed: float = -8.0,
        filtered: float = -3.0,
        min_age_seconds: float = 1200,
        stale_seconds: float = 3 * 86400,
        limit: int = 50,
    ) -> int:
        """Check comment/post status, then train the DQN on that outcome."""
        updated = 0
        self.last_status_updates = []
        try:
            self._ensure_db()
            with self._connect() as conn:
                try:
                    rows = conn.execute(
                        """
                        SELECT id, state_key, action, url, kind, created_at, state_json
                        FROM rl_pending
                        WHERE applied = 0
                        ORDER BY id ASC
                        LIMIT ?
                        """,
                        (max(1, int(limit)),),
                    ).fetchall()
                except Exception:
                    rows = conn.execute(
                        """
                        SELECT id, state_key, action, url, kind, created_at, '' AS state_json
                        FROM rl_pending
                        WHERE applied = 0
                        ORDER BY id ASC
                        LIMIT ?
                        """,
                        (max(1, int(limit)),),
                    ).fetchall()
            now = datetime.now()
            for row in rows:
                url = str(row["url"] or "")
                created_raw = str(row["created_at"] or "")
                age = 0.0
                if created_raw:
                    try:
                        age = (now - datetime.fromisoformat(created_raw)).total_seconds()
                    except Exception:
                        age = float(min_age_seconds)
                if age < float(min_age_seconds):
                    continue
                try:
                    raw = inspect_fn(url)
                except Exception:
                    raw = None
                info: Optional[Dict[str, Any]]
                if isinstance(raw, dict):
                    info = raw
                elif isinstance(raw, int):
                    info = {"status": "live", "score": raw}
                else:
                    info = None
                if info is None:
                    if age < float(stale_seconds):
                        continue
                    # Never graded and now too old to bother. Retire the row
                    # WITHOUT training — inventing a reward for an outcome we
                    # never observed is what poisons the model.
                    self._retire_unchecked(row["id"])
                    continue
                reward = reward_from_status(
                    info,
                    still_live=still_live,
                    score_2=score_2,
                    upvote_5=upvote_5,
                    upvote_20=upvote_20,
                    score_neg=score_neg,
                    downvote=downvote,
                    removed=removed,
                    filtered=filtered,
                )
                if reward is None:
                    if age < float(stale_seconds):
                        continue
                    self._retire_unchecked(row["id"])
                    continue
                state: Any = str(row["state_key"] or "")
                raw_json = ""
                try:
                    raw_json = str(row["state_json"] or "")
                except Exception:
                    raw_json = ""
                if raw_json:
                    try:
                        loaded = json.loads(raw_json)
                        if isinstance(loaded, dict):
                            state = loaded
                    except Exception:
                        pass
                self.update_q_value(
                    state, str(row["action"]), float(reward), None, graded=True
                )
                status = str(info.get("status") or "")
                score = int(info.get("score") or 0)
                try:
                    with self._connect() as conn:
                        conn.execute(
                            """
                            UPDATE rl_pending
                            SET applied = 1, status = ?, score = ?, reward = ?
                            WHERE id = ?
                            """,
                            (status, score, float(reward), row["id"]),
                        )
                except Exception:
                    try:
                        with self._connect() as conn:
                            conn.execute(
                                "UPDATE rl_pending SET applied = 1 WHERE id = ?",
                                (row["id"],),
                            )
                    except Exception:
                        pass
                self.last_status_updates.append(
                    {
                        "url": url,
                        "status": status,
                        "score": score,
                        "reward": float(reward),
                        "action": str(row["action"] or ""),
                        "kind": str(row["kind"] or ""),
                    }
                )
                updated += 1
        except Exception:
            return updated
        return updated

    def _retire_unchecked(self, row_id: Any) -> None:
        """Close a pending row we never managed to grade, training nothing."""
        self.unchecked_retired += 1
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE rl_pending SET applied = 1, status = 'unchecked' WHERE id = ?",
                    (row_id,),
                )
        except Exception:
            return

    def get_performance_report(self) -> Dict[str, Any]:
        avg = (self.total_reward / self.action_count) if self.action_count else 0.0
        rate = (self.total_success / self.action_count) if self.action_count else 0.0
        graded_avg = (
            (self.graded_reward / self.graded_actions) if self.graded_actions else 0.0
        )
        return {
            "kind": self.kind,
            "actions": self.action_count,
            "success": self.total_success,
            "fail": self.total_fail,
            "average_reward": avg,
            "success_rate": rate,
            # The only numbers that reflect real Reddit outcomes.
            "graded_actions": self.graded_actions,
            "graded_average_reward": graded_avg,
            "unchecked_retired": self.unchecked_retired,
            "epsilon": self.epsilon,
            "q_size": len(self.replay),
            "replay": len(self.replay),
            "train_steps": self.train_steps,
            "loss": self.last_loss,
            "prior_buckets": len(self.action_stats),
        }

    def install_handlers(self) -> None:
        if self._handlers_installed:
            return
        self._handlers_installed = True

        def _save_quiet(*_args: Any) -> None:
            try:
                self.save_model()
                self.save_experience_to_db()
                self.log_performance()
            except Exception:
                pass

        atexit.register(_save_quiet)

        def _handle(signum: int, _frame: Any) -> None:
            _save_quiet()
            raise SystemExit(128 + int(signum))

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handle)
            except Exception:
                pass


_AGENT: Optional[RLAgent] = None


def get_agent() -> Optional[RLAgent]:
    return _AGENT


def set_agent(agent: Optional[RLAgent]) -> None:
    global _AGENT
    _AGENT = agent


def parse_comment_action(action: str) -> Tuple[bool, str]:
    raw = (action or "skip").strip().lower()
    if raw == "skip" or raw.startswith("skip"):
        return False, "friendly"
    if raw.startswith("comment:"):
        return True, raw.split(":", 1)[-1] or "friendly"
    if raw in {"comment", "yes", "true"}:
        return True, "friendly"
    return False, "friendly"


def parse_join_action(action: str) -> str:
    raw = (action or "").strip().lower()
    if raw in {"join:lurk", "lurk", "skip"}:
        return "lurk"
    return "comment"
