# pylint: disable=R0201

import concurrent
from concurrent.futures import ProcessPoolExecutor

import scipy
import numpy as np
from madmom.features import (
    DBNDownBeatTrackingProcessor,
    RNNDownBeatProcessor,
    DBNBeatTrackingProcessor,
    RNNBeatProcessor,
    BeatTrackingProcessor,
)

from omnizart.io import load_audio
from omnizart.utils import get_logger

logger = get_logger("Beat Extraction")


class MadmomBeatTracking:
    """Extract beat information with madmom library.

    Three different beat tracking methods are used together for producing a more
    stable beat tracking result.
    """
    def __init__(self, num_threads=3, parallel_workers=3):
        self.num_threads = num_threads
        self.parallel_workers=parallel_workers

    def _get_dbn_down_beat(self, audio_data_in1, min_bpm_in=50, max_bpm_in=230):
        proccesor = DBNDownBeatTrackingProcessor(
            beats_per_bar=[3, 4, 5, 6, 7],
            min_bpm=min_bpm_in,
            max_bpm=max_bpm_in,
            fps=100,
            num_threads=self.num_threads
        )
        action = RNNDownBeatProcessor(num_threads=self.num_threads)(audio_data_in1)
        return proccesor(action)[:, 0]

    def _get_dbn_beat(self, audio_data_in2):
        proccesor = DBNBeatTrackingProcessor(fps=100, num_threads=self.num_threads)
        action = RNNBeatProcessor(num_threads=self.num_threads)(audio_data_in2)
        return proccesor(action)

    def _get_beat(self, audio_data_in3):
        proccesor = BeatTrackingProcessor(fps=100, num_threads=self.num_threads)
        action = RNNBeatProcessor(num_threads=self.num_threads)(audio_data_in3)
        return proccesor(action)

    def process(self, audio_data):
        """Generate beat tracking results with multiple approaches."""
        if self.parallel_workers == 0:
            # Run sequentially
            logger.debug("Running beat tracking sequentially...")
            pred_beats1 = self._get_dbn_down_beat(audio_data, min_bpm_in=50, max_bpm_in=230)
            pred_beats2 = self._get_dbn_beat(audio_data)
            pred_beats3 = self._get_beat(audio_data)
        else:
            with ProcessPoolExecutor(max_workers=self.parallel_workers) as executor:
                logger.debug("Submitting and executing parallel beat tracking jobs")
                future_1 = executor.submit(self._get_dbn_down_beat, audio_data, min_bpm_in=50, max_bpm_in=230)
                future_2 = executor.submit(self._get_dbn_beat, audio_data)
                future_3 = executor.submit(self._get_beat, audio_data)

                queue = {future_1: "dbn_down_beat", future_2: "dbn_beat", future_3: "beat"}

            results = {}
            for future in concurrent.futures.as_completed(queue, timeout=600):
                func_name = queue[future]
                results[func_name] = future.result()
                logger.debug("Job %s finished.", func_name)

            pred_beats1 = results["dbn_down_beat"]
            pred_beats2 = results["dbn_beat"]
            pred_beats3 = results["beat"]

        pred_beat_len1 = np.mean(
            np.sort(pred_beats1[1:] - pred_beats1[:-1])[int(len(pred_beats1) * 0.2):int(len(pred_beats1) * 0.8)]
        )
        pred_bpm1 = 60.0 / pred_beat_len1

        pred_beat_len2 = np.mean(
            np.sort(pred_beats2[1:] - pred_beats2[:-1])[int(len(pred_beats2) * 0.2):int(len(pred_beats2) * 0.8)]
        )
        pred_bpm2 = 60.0 / pred_beat_len2

        pred_beat_len3 = np.mean(
            np.sort(pred_beats3[1:] - pred_beats3[:-1])[int(len(pred_beats3) * 0.2):int(len(pred_beats3) * 0.8)]
        )
        pred_bpm3 = 60.0 / pred_beat_len3
        pred_bpm_avg = np.mean([pred_bpm1, pred_bpm2, pred_bpm3])

        logger.debug("Running last beat tracking step...")
        return self._get_dbn_down_beat(audio_data, min_bpm_in=pred_bpm_avg / 1.38, max_bpm_in=pred_bpm_avg * 1.38)


def extract_beat_with_madmom(audio_path, sampling_rate=44100, parallel_workers=3, num_threads=3):
    """Extract beat position (in seconds) of the audio.

    Extract beat with mixture of beat tracking techiniques using madmom.

    Parameters
    ----------
    audio_path: Path
        Path to the target audio
    sampling_rate: int
        Desired sampling to be resampled.

    Returns
    -------
    beat_arr: 1D numpy array
        Contains beat positions in seconds.
    audio_len_sec: float
        Total length of the audio in seconds.
    """
    logger.debug("Loading audio: %s", audio_path)
    audio_data, _ = load_audio(audio_path, sampling_rate=sampling_rate)
    logger.debug("Runnig beat tracking...")
    mbt = MadmomBeatTracking(num_threads=num_threads, parallel_workers=parallel_workers)
    return mbt.process(audio_data), len(audio_data) / sampling_rate


def extract_mini_beat_from_beat_arr(beat_arr, audio_len_sec, mini_beat_div_n=32):
    """Extract mini beats from the beat array.

    Furhter split beat into shorter beat interval, which we call it *mini beat*, to increase the
    beat resolution. We use linear interpolation to generate the mini beats.

    Parameters
    ----------
    beat_arr: 1D numpy array
        Beat array generated by `extract_beat_with_madmom`.
    audio_len_sec: float
        Total length of the audio in seconds.
    mini_beat_div_n: int
        Number of mini beats in a single 4/4 measure.

    Returns
    -------
    mini_beat_pos_t: 1D numpy array
        Positions of mini beats in seconds.

    """
    # How many division per bar on the 4/4 time signiture basis
    mini_beat_div_n = np.round(mini_beat_div_n / 4).astype("int")

    beat_time_ary_in = np.array(beat_arr)
    beat_abs_idx = np.arange(len(beat_arr)) + 1  # count from 1
    beat_map_func = scipy.interpolate.interp1d(beat_abs_idx, beat_time_ary_in, fill_value="extrapolate")

    mini_beat_abs_idx = np.arange(0, beat_arr.shape[0] + 1, (1 / mini_beat_div_n))
    mini_beat_pos_t = [beat_map_func(x) for x in mini_beat_abs_idx]

    # Filter out beat outside audio time range
    mini_beat_pos_t = np.array([x for x in mini_beat_pos_t if x >= 0])
    mini_beat_pos_t = np.array([x for x in mini_beat_pos_t if x <= audio_len_sec])

    return mini_beat_pos_t


def extract_mini_beat_from_audio_path(audio_path, sampling_rate=44100, mini_beat_div_n=32, parallel_workers=3, num_threads=3):
    """ Wrapper of extracting mini beats from audio path. """
    logger.debug("Extracting beat with madmom")
    beat_arr, audio_len_sec = extract_beat_with_madmom(
        audio_path, 
        sampling_rate=sampling_rate,
        parallel_workers=parallel_workers,
        num_threads=num_threads
    )
    logger.debug("Extracting mini beat")
    return extract_mini_beat_from_beat_arr(beat_arr, audio_len_sec, mini_beat_div_n=mini_beat_div_n)


if __name__ == "__main__":
    AUDIO_PATH = "checkpoints/Last Stardust - piano.wav"
    mini_beat_arr = extract_mini_beat_from_audio_path(AUDIO_PATH)
