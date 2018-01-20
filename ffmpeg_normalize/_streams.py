import os
import re
import json

from ._errors import FFmpegNormalizeError
from ._cmd_utils import NUL, run_command, ffmpeg_exe, dict_to_filter_opts
from ._logger import setup_custom_logger
logger = setup_custom_logger('ffmpeg_normalize')

class MediaStream(object):
    def __init__(self, media_file, stream_type, stream_id):
        """
        Arguments:
            media_file {MediaFile} -- parent media file
            stream_type {str} -- stream type
            stream_id {int} -- Audio stream id
        """
        self.media_file = media_file
        self.stream_type = stream_type
        self.stream_id = stream_id

    def __repr__(self):
        return "<{}, {} stream {}>".format(
            os.path.basename(self.media_file.input_file), self.stream_type, self.stream_id
        )

class VideoStream(MediaStream):
    def __init__(self, media_file, stream_id):
        super(VideoStream, self).__init__(media_file, 'video', stream_id)

class SubtitleStream(MediaStream):
    def __init__(self, media_file, stream_id):
        super(SubtitleStream, self).__init__(media_file, 'subtitle', stream_id)

class AudioStream(MediaStream):
    def __init__(self, media_file, stream_id, sample_rate=None, bit_depth=None):
        super(AudioStream, self).__init__(media_file, 'audio', stream_id)

        self.loudness_statistics = {
            'ebu': None,
            'mean': None,
            'max': None
        }

        self.sample_rate = sample_rate
        self.bit_depth = bit_depth

    def __repr__(self):
        return "<{}, audio stream {}>".format(
            os.path.basename(self.media_file.input_file), self.stream_id
        )

    def get_pcm_codec(self):
        if not self.bit_depth:
            return 'pcm_s16le'
        elif self.bit_depth <= 8:
            return 'pcm_s8'
        elif self.bit_depth in [16, 24, 32, 64]:
            return 'pcm_s{}le'.format(self.bit_depth)
        else:
            logger.warning(
                "Unsupported bit depth {}, falling back to pcm_s16le".format(self.bit_depth)
            )
            return 'pcm_s16le'

    def parse_volumedetect_stats(self):
        """
        Use ffmpeg with volumedetect filter to get the mean volume of the input file.
        """
        logger.info(
            "Running first pass volumedetect filter for stream {}".format(self.stream_id)
        )

        filter_str = '[0:{}]volumedetect'.format(self.stream_id)

        cmd = [
            ffmpeg_exe, '-nostdin', '-y',
            '-i', self.media_file.input_file,
            '-filter_complex', filter_str,
            '-vn', '-sn', '-f', 'null', NUL
        ]

        output = run_command(cmd)

        logger.debug("Volumedetect command output:")
        logger.debug(output)

        mean_volume_matches = re.findall(r"mean_volume: ([\-\d\.]+) dB", output)
        if mean_volume_matches:
            self.loudness_statistics['mean'] = float(mean_volume_matches[0])
        else:
            raise FFmpegNormalizeError(
                "Could not get mean volume for {}".format(self.media_file.input_file)
            )

        max_volume_matches = re.findall(r"max_volume: ([\-\d\.]+) dB", output)
        if max_volume_matches:
            self.loudness_statistics['max'] = float(max_volume_matches[0])
        else:
            raise FFmpegNormalizeError(
                "Could not get max volume for {}".format(self.media_file.input_file)
            )

    def parse_loudnorm_stats(self):
        """
        Run a first pass loudnorm filter to get measured data.
        """
        logger.info(
            "Running first pass loudnorm filter for stream {}".format(self.stream_id)
        )

        opts = {
            'i': self.media_file.ffmpeg_normalize.target_level,
            'lra': self.media_file.ffmpeg_normalize.loudness_range_target,
            'tp': self.media_file.ffmpeg_normalize.true_peak,
            'offset': self.media_file.ffmpeg_normalize.offset,
            'print_format': 'json'
        }

        if self.media_file.ffmpeg_normalize.dual_mono:
            opts['dual_mono'] = 'true'

        filter_str = '[0:{}]'.format(self.stream_id) + \
            'loudnorm=' + dict_to_filter_opts(opts)

        cmd = [
            ffmpeg_exe, '-nostdin', '-y',
            '-i', self.media_file.input_file,
            '-filter_complex', filter_str,
            '-vn', '-sn', '-f', 'null', NUL
        ]

        output = run_command(cmd)

        logger.debug("Loudnorm first pass command output:")
        logger.debug(output)

        output_lines = [line.strip() for line in output.split('\n')]
        loudnorm_start = False
        for index, line in enumerate(output_lines):
            if line.startswith('[Parsed_loudnorm'):
                loudnorm_start = index
                break

        if not loudnorm_start:
            raise FFmpegNormalizeError("Could not parse loudnorm stats")

        try:
            loudnorm_stats = json.loads('\n'.join(output_lines[loudnorm_start + 1:]))
        except Exception as e:
            raise FFmpegNormalizeError("Could not parse loudnorm stats")

        logger.debug("Loudnorm stats parsed: {}".format(json.dumps(loudnorm_stats)))

        self.loudness_statistics['ebu'] = loudnorm_stats

    def get_second_pass_opts_ebu(self):
        """
        Return second pass loudnorm filter options string for ffmpeg
        """

        if not self.loudness_statistics['ebu']:
            raise FFmpegNormalizeError(
                "First pass not run, you must call parse_loudnorm_stats first"
            )

        opts = {
            'i': self.media_file.ffmpeg_normalize.target_level,
            'lra': self.media_file.ffmpeg_normalize.loudness_range_target,
            'tp': self.media_file.ffmpeg_normalize.true_peak,
            'offset': self.media_file.ffmpeg_normalize.offset,
            'measured_i': float(self.loudness_statistics['ebu']['input_i']),
            'measured_lra': float(self.loudness_statistics['ebu']['input_lra']),
            'measured_tp': float(self.loudness_statistics['ebu']['input_tp']),
            'measured_thresh': float(self.loudness_statistics['ebu']['input_thresh']),
            'linear': 'true',
            'print_format': 'json'
        }

        if self.media_file.ffmpeg_normalize.dual_mono:
            opts['dual_mono'] = 'true'

        return 'loudnorm=' + dict_to_filter_opts(opts)

    def get_second_pass_opts_peakrms(self):
        """
        Set the adjustment gain based on chosen option and mean/max volume,
        return the matching ffmpeg volume filter.
        """
        normalization_type = self.media_file.ffmpeg_normalize.normalization_type
        target_level = self.media_file.ffmpeg_normalize.target_level

        if normalization_type == 'peak':
            adjustment = 0 + target_level - \
                self.loudness_statistics['max']
        elif normalization_type == 'rms':
            adjustment = target_level - \
                self.loudness_statistics['mean']
        else:
            raise FFmpegNormalizeError(
                "Can only set adjustment for peak and RMS normalization"
            )

        logger.info(
            "Adjusting stream {} by {} dB to reach {}"
            .format(self.stream_id, adjustment, target_level)
        )

        if self.loudness_statistics['max'] + adjustment > 0:
            logger.warning(
                "Adjusting will lead to clipping of {} dB"
                .format(self.loudness_statistics['max'] + adjustment)
            )

        return 'volume={}'.format(adjustment)
