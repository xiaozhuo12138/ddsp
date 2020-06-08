# Copyright 2020 The DDSP Authors.
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

# Lint as: python3
"""Library of performance metrics relevant to DDSP training."""

from absl import logging
import ddsp
import librosa
import numpy as np
import tensorflow.compat.v2 as tf

# Global values for evaluation.
MIN_F0_CONFIDENCE = 0.85
OUTLIER_MIDI_THRESH = 12



# ---------------------- Helper Functions --------------------------------------
def squeeze(input_vector):
  """Ensure vector only has one axis of dimensionality."""
  if input_vector.ndim > 1:
    return np.squeeze(input_vector)
  else:
    return input_vector


def l1_distance(prediction, ground_truth):
  """Average L1 distance difference between two 1-D vectors."""
  prediction, ground_truth = np.squeeze(prediction), np.squeeze(ground_truth)
  min_length = min(prediction.size, ground_truth.size)
  diff = prediction[:min_length] - ground_truth[:min_length]
  return np.abs(diff)


def is_outlier(ground_truth_f0_conf):
  """Determine if ground truth f0 for audio sample is an outlier."""
  ground_truth_f0_conf = squeeze(ground_truth_f0_conf)
  return np.max(ground_truth_f0_conf) < MIN_F0_CONFIDENCE


def compute_audio_features(audio,
                           n_fft=2048,
                           sample_rate=16000,
                           frame_rate=250):
  """Compute features from audio."""
  audio_feats = {'audio': audio}
  audio = squeeze(audio)

  audio_feats['loudness_db'] = ddsp.spectral_ops.compute_loudness(
      audio, sample_rate, frame_rate, n_fft)

  audio_feats['f0_hz'], audio_feats['f0_confidence'] = (
      ddsp.spectral_ops.compute_f0(audio, sample_rate, frame_rate))

  return audio_feats


def f0_dist_conf_thresh(f0_hz,
                        f0_hz_gen,
                        f0_confidence,
                        f0_confidence_thresh=MIN_F0_CONFIDENCE):
  """Compute L1 between gen audio and ground truth audio.

  Calculating F0 distance is more complicated than calculating loudness
  distance because of inherent inaccuracies in pitch tracking.

  We take the following steps:
  - Define a `keep_mask` that only select f0 values above when f0_confidence in
  the original audio exceeds a minimum threshold.
  Experimentation by jessengel@ and hanoih@ found this to be optimal way to
  filter out bad f0 pitch tracking.
  - Compute `delta_f0` between generated audio and ground truth audio.
  - Only select values in `delta_f0` based on this `keep_mask`
  - Compute mean on this selection
  - At the start of training, audio samples will sound bad and thus have no
  pitch content. If the `f0_confidence` is all below the threshold, we keep a
  count of it. A better performing model will have a smaller count of
  "untrackable pitch" samples.

  Args:
    f0_hz: Ground truth audio f0 in hertz [MB,:].
    f0_hz_gen: Generated audio f0 in hertz [MB,:].
    f0_confidence: Ground truth audio f0 confidence [MB,:]
    f0_confidence_thresh: Confidence threshold above which f0 metrics will be
      computed

  Returns:
    delta_f0_mean: Float or None if entire generated sample had
      f0_confidence below threshold. In units of MIDI (logarithmic frequency).
  """
  if len(f0_hz.shape) > 2:
    f0_hz = f0_hz[:, :, 0]
  if len(f0_hz_gen.shape) > 2:
    f0_hz_gen = f0_hz_gen[:, :, 0]
  if len(f0_confidence.shape) > 2:
    f0_confidence = f0_confidence[:, :, 0]

  if np.max(f0_confidence) < f0_confidence_thresh:
    # Generated audio is not good enough for reliable pitch tracking.
    return None
  else:
    keep_mask = f0_confidence >= f0_confidence_thresh

    # Report mean error in midi space for easier interpretation.
    f0_midi = librosa.core.hz_to_midi(f0_hz)
    f0_midi_gen = librosa.core.hz_to_midi(f0_hz_gen)
    # Set -infs introduced by hz_to_midi to 0.
    f0_midi[f0_midi == -np.inf] = 0
    f0_midi_gen[f0_midi_gen == -np.inf] = 0

    delta_f0_midi = l1_distance(f0_midi, f0_midi_gen)
    delta_f0_midi_filt = delta_f0_midi[keep_mask]
    return np.mean(delta_f0_midi_filt)


# ---------------------- Metrics -----------------------------------------------
class BaseMetrics(object):
  """Base object for computing metrics on generated audio samples."""

  def __init__(self, sample_rate, frame_rate, metrics_name):
    """Constructor.

    Args:
      sample_rate: audio sample rate
      frame_rate: feature frame rate
      metrics_name: name to printed when logging
    """
    self._sample_rate = sample_rate
    self._frame_rate = frame_rate
    self._metrics_name = metrics_name
    self._metrics = {}

  def flush(self, step):
    """Add summaries for each metric and reset the state."""
    if bool(self._metrics):
      # Log metrics
      logging.info('Computing %s metrics complete. Flushing all metrics',
                   self._metrics_name)
      metrics_str = ' | '.join(
          '{}: {:0.3f}'.format(k, v.result()) for k, v in self._metrics.items())
      logging.info(metrics_str)

      # Write tf summaries.
      for name, metric in self._metrics.items():
        tf.summary.scalar('metrics/{}'.format(name), metric.result(), step)
        metric.reset_states()


class LoudnessMetrics(BaseMetrics):
  """Helper object for computing loudness metrics."""

  def __init__(self, sample_rate, frame_rate):
    super().__init__(
        sample_rate=sample_rate, frame_rate=frame_rate, metrics_name='Loudness')
    self.metrics = {
        'loudness_db': tf.keras.metrics.Mean('loudness_db'),
    }

  def update_state(self, batch, audio_gen):
    """Update metrics based on a batch of audio.

    Args:
      batch: Dictionary of input features.
      audio_gen: Batch of generated audio.
    """
    loudness_original = batch['loudness_db']
    # Compute loudness across entire batch
    loudness_gen = ddsp.spectral_ops.compute_loudness(
        audio_gen, sample_rate=self._sample_rate, frame_rate=self._frame_rate)

    batch_size = int(audio_gen.shape[0])
    for i in range(batch_size):
      ld_dist = np.mean(l1_distance(loudness_original[i], loudness_gen[i]))
      self.metrics['loudness_db'].update_state(ld_dist)
      logging.info('sample {} | ld_dist(db): {:.3f}'.format(i, ld_dist))


class F0CrepeMetrics(BaseMetrics):
  """Helper object for computing CREPE-based f0 metrics.

  Note that batch operations are not possible when CREPE has viterbi argument
  set to True.
  """

  def __init__(self, sample_rate, frame_rate):
    super().__init__(
        sample_rate=sample_rate, frame_rate=frame_rate, metrics_name='F0Crepe')
    self._metrics = {
        'f0_crepe':
            tf.keras.metrics.Mean('f0_crepe'),
        'f0_crepe_outlier_ratio':
            tf.keras.metrics.Accuracy('f0_crepe_outlier_ratio'),
    }

  def update_state(self, batch, audio_gen):
    """Update metrics based on a batch of audio.

    Args:
      batch: Dictionary of input features.
      audio_gen: Batch of generated audio.
    """
    batch_size = int(audio_gen.shape[0])
    # Compute metrics per sample. No batch operations possible.
    for i in range(batch_size):
      # Extract f0 from generated audio example.
      f0_hz_gen, _ = ddsp.spectral_ops.compute_f0(
          audio_gen[i],
          sample_rate=self._sample_rate,
          frame_rate=self._frame_rate,
          viterbi=True)
      f0_hz_original = batch['f0_hz'][i]
      f0_conf_original = batch['f0_confidence'][i]

      if is_outlier(f0_conf_original):
        # Ground truth f0 was unreliable to begin with. Discard.
        f0_crepe_dist = None
      else:
        # Gound truth f0 was reliable, compute f0 distance with generated audio
        f0_crepe_dist = f0_dist_conf_thresh(f0_hz_original, f0_hz_gen,
                                            f0_conf_original)
        if f0_crepe_dist is None or f0_crepe_dist > OUTLIER_MIDI_THRESH:
          # Generated audio had untrackable pitch content or is an outlier.
          self.metrics['f0_crepe_outlier_ratio'].update_state(True, True)
          logging.info('Sample %d has untrackable pitch content', i)
        else:
          # Generated audio had trackable pitch content and is within tolerance
          self.metrics['f0_crepe'].update_state(f0_crepe_dist)
          self.metrics['f0_crepe_outlier_ratio'].update_state(True, False)
          logging.info('sample {} | f0_crepe_dist(midi): {:.3f}'.format(
              i, f0_crepe_dist))

  def flush(self, step):
    """Perform additional step of resetting CREPE."""
    super().flush(step)
    ddsp.spectral_ops.reset_crepe()  # Reset CREPE global state


class F0EncoderMetrics(BaseMetrics):
  """Helper object for computing f0 encoder metrics."""

  def __init__(self, sample_rate, frame_rate):
    super().__init__(
        sample_rate=sample_rate,
        frame_rate=frame_rate,
        metrics_name='F0Encoder')
    self._metrics = {'f0_encoder': tf.keras.metrics.Mean('f0_encoder')}

  def update_state(self, batch, f0_hz_predict):
    """Update metrics based on a batch of audio.

    Args:
      batch: Dictionary of input features.
      f0_hz_predict: Batch of encoded f0, same as input f0 if no f0 encoder.
    """

    batch_size = int(f0_hz_predict.shape[0])
    # Compute metrics per sample. No batch operations possible.
    for i in range(batch_size):
      f0_hz_original = batch['f0_hz'][i]
      f0_conf_original = batch['f0_confidence'][i]

      if not is_outlier(f0_conf_original):
        # Gound truth f0 was reliable, proceed with metrics
        # Compute distance between original f0_hz labels and f0 encoder values.
        # Resample if f0 encoder has different number of time steps.
        f0_hz_encoder = f0_hz_predict[i]
        if f0_hz_encoder.shape[0] != f0_hz_original.shape[0]:
          f0_hz_encoder = ddsp.core.resample(f0_hz_encoder,
                                             f0_hz_original.shape[0])
        f0_encoder_dist = f0_dist_conf_thresh(f0_hz_original, f0_hz_encoder,
                                              f0_conf_original)
        self._metrics['f0_encoder'].update_state(f0_encoder_dist)
        logging.info('sample {} | f0_encoder_dist(midi): {:.3f}'.format(
            i, f0_encoder_dist))
