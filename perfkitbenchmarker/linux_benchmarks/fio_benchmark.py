# Copyright 2014 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Runs fio benchmarks.

Man: http://manpages.ubuntu.com/manpages/natty/man1/fio.1.html
Quick howto: http://www.bluestop.org/fio/HOWTO.txt
"""


import collections
import json
import logging
import posixpath
import re
import time
from absl import flags
import jinja2

from perfkitbenchmarker import configs
from perfkitbenchmarker import data
from perfkitbenchmarker import errors
from perfkitbenchmarker import flag_util
from perfkitbenchmarker import sample
from perfkitbenchmarker import units
from perfkitbenchmarker import vm_util
from perfkitbenchmarker.linux_packages import fio

PKB_FIO_LOG_FILE_NAME = 'pkb_fio_avg'
LOCAL_JOB_FILE_SUFFIX = '_fio.job'  # used with vm_util.PrependTempDir()
REMOTE_JOB_FILE_PATH = posixpath.join(vm_util.VM_TMP_DIR, 'fio.job')
DEFAULT_TEMP_FILE_NAME = 'fio-temp-file'
MOUNT_POINT = '/scratch'


# This dictionary maps scenario names to dictionaries of fio settings.
SCENARIOS = {
    'sequential_write': {
        'name': 'sequential_write',
        'rwkind': 'write',
        'blocksize': '512k'
    },
    'sequential_read': {
        'name': 'sequential_read',
        'rwkind': 'read',
        'blocksize': '512k'
    },
    'random_write': {
        'name': 'random_write',
        'rwkind': 'randwrite',
        'blocksize': '4k'
    },
    'random_read': {
        'name': 'random_read',
        'rwkind': 'randread',
        'blocksize': '4k'
    },
    'random_read_write': {
        'name': 'random_read_write',
        'rwkind': 'randrw',
        'blocksize': '4k'
    },
    'sequential_trim': {
        'name': 'sequential_trim',
        'rwkind': 'trim',
        'blocksize': '512k'
    },
    'rand_trim': {
        'name': 'rand_trim',
        'rwkind': 'randtrim',
        'blocksize': '4k'
    }
}


FLAGS = flags.FLAGS

# Modes for --fio_target_mode
AGAINST_FILE_WITH_FILL_MODE = 'against_file_with_fill'
AGAINST_FILE_WITHOUT_FILL_MODE = 'against_file_without_fill'
AGAINST_DEVICE_WITH_FILL_MODE = 'against_device_with_fill'
AGAINST_DEVICE_WITHOUT_FILL_MODE = 'against_device_without_fill'
AGAINST_DEVICE_MODES = {AGAINST_DEVICE_WITH_FILL_MODE,
                        AGAINST_DEVICE_WITHOUT_FILL_MODE}
FILL_TARGET_MODES = {AGAINST_DEVICE_WITH_FILL_MODE,
                     AGAINST_FILE_WITH_FILL_MODE}


flags.DEFINE_string('fio_jobfile', None,
                    'Job file that fio will use. If not given, use a job file '
                    'bundled with PKB. Cannot use with '
                    '--fio_generate_scenarios.')
flags.DEFINE_list('fio_generate_scenarios', [],
                  'Generate a job file with the given scenarios. Special '
                  'scenario \'all\' generates all scenarios. Available '
                  'scenarios are sequential_write, sequential_read, '
                  'random_write, and random_read. Cannot use with '
                  '--fio_jobfile.   You can also specify a scenario in the '
                  'format accesspattern_blocksize_operation_workingset '
                  'for a custom workload.')
flags.DEFINE_bool('fio_use_default_scenarios', True,
                  'Use the legacy scenario tables defined in fio_benchmark.py '
                  'to resolve the scenario name in generate scenarios')
flags.DEFINE_enum('fio_target_mode', AGAINST_FILE_WITHOUT_FILL_MODE,
                  [AGAINST_DEVICE_WITH_FILL_MODE,
                   AGAINST_DEVICE_WITHOUT_FILL_MODE,
                   AGAINST_FILE_WITH_FILL_MODE,
                   AGAINST_FILE_WITHOUT_FILL_MODE],
                  'Whether to run against a raw device or a file, and whether '
                  'to prefill.')
flags.DEFINE_string('fio_fill_size', '100%',
                    'The amount of device to fill in prepare stage. '
                    'The valid value can either be an integer, which '
                    'represents the number of bytes to fill or a '
                    'percentage, which represents the percentage '
                    'of the device. A filesystem will be unmounted before '
                    'filling and remounted afterwards. Only valid when '
                    '--fio_target_mode is against_device_with_fill or '
                    'against_file_with_fill.')
flag_util.DEFINE_integerlist('fio_io_depths', flag_util.IntegerList([1]),
                             'IO queue depths to run on. Can specify a single '
                             'number, like --fio_io_depths=1, a range, like '
                             '--fio_io_depths=1-4, or a list, like '
                             '--fio_io_depths=1-4,6-8',
                             on_nonincreasing=flag_util.IntegerListParser.WARN,
                             module_name=__name__)
flag_util.DEFINE_integerlist('fio_num_jobs', flag_util.IntegerList([1]),
                             'Number of concurrent fio jobs to run.',
                             on_nonincreasing=flag_util.IntegerListParser.WARN,
                             module_name=__name__)
flags.DEFINE_integer('fio_working_set_size', None,
                     'The size of the working set, in GB. If not given, use '
                     'the full size of the device. If using '
                     '--fio_generate_scenarios and not running against a raw '
                     'device, you must pass --fio_working_set_size.',
                     lower_bound=0)
flag_util.DEFINE_units('fio_blocksize', None,
                       'The block size for fio operations. Default is given by '
                       'the scenario when using --generate_scenarios. This '
                       'flag does not apply when using --fio_jobfile.',
                       convertible_to=units.byte)
flags.DEFINE_integer('fio_runtime', 600,
                     'The number of seconds to run each fio job for.',
                     lower_bound=1)
flags.DEFINE_list('fio_parameters', ['randrepeat=0'],
                  'Parameters to apply to all PKB generated fio jobs. Each '
                  'member of the list should be of the form "param=value".')
flags.DEFINE_boolean('fio_lat_log', False,
                     'Whether to collect a latency log of the fio jobs.')
flags.DEFINE_boolean('fio_bw_log', False,
                     'Whether to collect a bandwidth log of the fio jobs.')
flags.DEFINE_boolean('fio_iops_log', False,
                     'Whether to collect an IOPS log of the fio jobs.')
flags.DEFINE_integer('fio_log_avg_msec', 1000,
                     'By default, this will average each log entry in the '
                     'fio latency, bandwidth, and iops logs over the specified '
                     'period of time in milliseconds. If set to 0, fio will '
                     'log an entry for every IO that completes, this can grow '
                     'very quickly in size and can cause performance overhead.',
                     lower_bound=0)
flags.DEFINE_boolean('fio_hist_log', False,
                     'Whether to collect clat histogram.')
flags.DEFINE_integer('fio_log_hist_msec', 1000,
                     'Same as fio_log_avg_msec, but logs entries for '
                     'completion latency histograms. If set to 0, histogram '
                     'logging is disabled.')
flags.DEFINE_boolean(  # TODO(user): Add support for simultaneous read.
    'fio_write_against_multiple_clients', False,
    'Whether to run fio against multiple clients. Only applicable '
    'when running fio against network mounts and rw=write.')
flags.DEFINE_integer('fio_command_timeout_sec', None,
                     'Timeout for fio commands in seconds.')
flags.DEFINE_enum('fio_rng', 'tausworthe64',
                  ['tausworthe', 'lfsr', 'tausworthe64'],
                  'Which RNG to use for 4k Random IOPS.')
_DIRECT_IO = flags.DEFINE_boolean(
    'fio_direct', True,
    'Whether to use O_DIRECT to bypass OS cache. This is strongly '
    'recommended, but not supported by all files.')


FLAGS_IGNORED_FOR_CUSTOM_JOBFILE = {
    'fio_generate_scenarios', 'fio_io_depths', 'fio_runtime',
    'fio_blocksize', 'fio_num_jobs', 'fio_parameters'}


def AgainstDevice():
  """Check whether we're running against a device or a file.

  Returns:
    True if running against a device, False if running against a file.
  """
  return FLAGS.fio_target_mode in AGAINST_DEVICE_MODES


def FillTarget():
  """Check whether we should pre-fill our target or not.

  Returns:
    True if we should pre-fill our target, False if not.
  """
  return FLAGS.fio_target_mode in FILL_TARGET_MODES


def FillDevice(vm, disk, fill_size, exec_path):
  """Fill the given disk on the given vm up to fill_size.

  Args:
    vm: a linux_virtual_machine.BaseLinuxMixin object.
    disk: a disk.BaseDisk attached to the given vm.
    fill_size: amount of device to fill, in fio format.
    exec_path: string path to the fio executable
  """

  command = (('%s --filename=%s --ioengine=libaio '
              '--name=fill-device --blocksize=512k --iodepth=64 '
              '--rw=write --direct=1 --size=%s') %
             (exec_path, disk.GetDevicePath(), fill_size))

  vm.RobustRemoteCommand(command)


BENCHMARK_NAME = 'fio'
BENCHMARK_CONFIG = """
fio:
  description: Runs fio in sequential, random, read and write modes.
  vm_groups:
    default:
      vm_spec: *default_single_core
      disk_spec: *default_500_gb
      vm_count: null
"""


JOB_FILE_TEMPLATE = """
[global]
ioengine=libaio
invalidate=1
direct={{direct}}
runtime={{runtime}}
time_based
filename={{filename}}
do_verify=0
verify_fatal=0
group_reporting=1
{%- for parameter in parameters %}
{{parameter}}
{%- endfor %}
{%- for scenario in scenarios %}
{%- for numjob in numjobs %}
{%- for iodepth in iodepths %}

[{{scenario['name']}}-io-depth-{{iodepth}}-num-jobs-{{numjob}}]
stonewall
rw={{scenario['rwkind']}}
{%- if scenario['rwmixread'] is defined %}
rwmixread={{scenario['rwmixread']}}
{%- endif%}
{%- if scenario['rwmixwrite'] is defined %}
rwmixwrite={{scenario['rwmixwrite']}}
{%- endif%}
{%- if scenario['fsync'] is defined %}
fsync={{scenario['fsync']}}
{%- endif%}
{%- if scenario['blocksize'] is defined %}
blocksize={{scenario['blocksize']}}
{%- elif scenario['bssplit'] is defined %}
bssplit={{scenario['bssplit']}}
{%- endif%}
iodepth={{iodepth}}
size={{scenario['size']}}
numjobs={{numjob}}
{%- endfor %}
{%- endfor %}
{%- endfor %}
"""

SECONDS_PER_MINUTE = 60

# known rwkind fio parameters
RWKIND_SEQUENTIAL_READ = 'read'
RWKIND_SEQUENTIAL_WRITE = 'write'
RWKIND_RANDOM_READ = 'randread'
RWKIND_RANDOM_WRITE = 'randwrite'
RWKIND_SEQUENTIAL_READ_WRITE = 'rw'  # 'readwrite' is also valid
RWKIND_RANDOM_READ_WRITE = 'randrw'
RWKIND_SEQUENTIAL_TRIM = 'trim'

# define fragments from scenario_strings
OPERATION_READ = 'read'
OPERATION_WRITE = 'write'
OPERATION_TRIM = 'trim'
OPERATION_READWRITE = 'readwrite'   # mixed read and writes
ALL_OPERATIONS = frozenset([
    OPERATION_READ, OPERATION_WRITE, OPERATION_TRIM, OPERATION_READWRITE])

ACCESS_PATTERN_SEQUENTIAL = 'seq'
ACCESS_PATTERN_RANDOM = 'rand'
ALL_ACCESS_PATTERNS = frozenset([
    ACCESS_PATTERN_SEQUENTIAL, ACCESS_PATTERN_RANDOM])

# map from scenario_string fragments to rwkind fio parameter
MAP_ACCESS_OP_TO_RWKIND = {
    (ACCESS_PATTERN_SEQUENTIAL, OPERATION_READ): RWKIND_SEQUENTIAL_READ,
    (ACCESS_PATTERN_SEQUENTIAL, OPERATION_WRITE): RWKIND_SEQUENTIAL_WRITE,
    (ACCESS_PATTERN_RANDOM, OPERATION_READ): RWKIND_RANDOM_READ,
    (ACCESS_PATTERN_RANDOM, OPERATION_WRITE): RWKIND_RANDOM_WRITE,
    (ACCESS_PATTERN_SEQUENTIAL, OPERATION_READWRITE):
        RWKIND_SEQUENTIAL_READ_WRITE,
    (ACCESS_PATTERN_RANDOM, OPERATION_READWRITE): RWKIND_RANDOM_READ_WRITE,
    (ACCESS_PATTERN_SEQUENTIAL, RWKIND_SEQUENTIAL_TRIM): RWKIND_SEQUENTIAL_TRIM,
}

# check for known fields, as the JOB_FILE_TEMPLATE looks for these
# fields explicitly and needs to be updated
FIO_KNOWN_FIELDS_IN_JINJA = [
    'rwmixread',
    'rwmixwrite',
    'fsync',
]


def _IsBlockSizeASplitSpecification(blocksize_str: str) -> bool:
  """determines if a blocksize_str looks like a bssplit parameter.

  an example parameter would be:

  format is blocksize/percent:blocksize/percent:...blocksize/percent

  e.g.
  8k/28:12k/23:4k/23:16k/7:20k/2:32k/17

  This is just a heuristic.

  Args:
    blocksize_str:  either a blocksize (like 4k) or a bssplit parameter

  Returns:
    True if this is split specification, false if a single block size.
  """
  return ':' in blocksize_str


def GetScenarioFromScenarioString(scenario_string):
  """Extract rwkind,blocksize,size from scenario string."""
  # look for legacy entries in the scenario map first
  result = (SCENARIOS.get(scenario_string, None)
            if FLAGS.fio_use_default_scenarios else None)
  if result:
    # return a copy so that the scenario can be mutated further if needed
    # without modifying the SCENARIO map
    return result.copy()

  # decode the parameters from the scenario name
  # in the format accesspattern_blocksize_operation_workingset
  # example pattern would be:
  #    rand_16k_write_100%
  #    seq_1M_write_100%
  fields = scenario_string.split('_')
  if len(fields) < 4:
    raise errors.Setup.InvalidFlagConfigurationError(
        f'Unexpected Scenario string format: {scenario_string}')
  (access_pattern, blocksize_str, operation, workingset_str) = fields[0:4]

  if access_pattern not in ALL_ACCESS_PATTERNS:
    raise errors.Setup.InvalidFlagConfigurationError(
        f'Unexpected access pattern {access_pattern} '
        f'in scenario {scenario_string}')

  if operation not in ALL_OPERATIONS:
    raise errors.Setup.InvalidFlagConfigurationError(
        f'Unexpected operation {operation}'
        f'in scenario {scenario_string}')

  access_op = (access_pattern, operation)
  rwkind = MAP_ACCESS_OP_TO_RWKIND.get(access_op, None)
  if not rwkind:
    raise errors.Setup.InvalidFlagConfigurationError(
        f'{access_pattern} and {operation} could not be mapped '
        f'to a rwkind fio parameter from '
        f'scenario {scenario_string}')

  # required fields of JOB_FILE_TEMPLATE
  result = {
      'name': scenario_string.replace(',', '__'),
      'rwkind': rwkind,
      'size': workingset_str
    }

  if _IsBlockSizeASplitSpecification(blocksize_str):
    result['bssplit'] = blocksize_str
  else:
    result['blocksize'] = blocksize_str

  # The first four fields are well defined - after that, we use
  # key value pairs to encode any extra fields we need
  # The format is key-value for any additional fields appended
  # e.g. rand_16k_readwrite_5TB_rwmixread-65
  #          random access pattern
  #          16k block size
  #          readwrite operation
  #          5 TB working set
  #          rwmixread of 65. (so 65% reads and 35% writes)
  for extra_fields in fields[4:]:
    key_value = extra_fields.split('-')
    key = key_value[0]
    value = key_value[1]
    if key not in FIO_KNOWN_FIELDS_IN_JINJA:
      raise errors.Setup.InvalidFlagConfigurationError(
          'Unregonzied FIO parameter {0} out of scenario {1}'.format(
              key, scenario_string))
    result[key] = value

  return result


def GenerateJobFileString(filename, scenario_strings,
                          io_depths, num_jobs, working_set_size,
                          block_size, runtime, direct, parameters):
  """Make a string with our fio job file.

  Args:
    filename: the file or disk we pre-filled, if any.
    scenario_strings: list of strings with names in SCENARIOS.
    io_depths: iterable of integers. The IO queue depths to test.
    num_jobs: iterable of integers. The number of fio processes to test.
    working_set_size: int or None. If int, the size of the working set in GB.
    block_size: Quantity or None. If quantity, the block size to use.
    runtime: int. The number of seconds to run each job.
    direct: boolean. Whether to use direct IO.
    parameters: list. Other fio parameters to be applied to all jobs.

  Returns:
    The contents of a fio job file, as a string.
  """

  if 'all' in scenario_strings and FLAGS.fio_use_default_scenarios:
    scenarios = SCENARIOS.values()
  else:
    scenarios = [GetScenarioFromScenarioString(scenario_string.strip('"'))
                 for scenario_string in scenario_strings]

  default_size_string = (str(working_set_size) + 'G'
                         if working_set_size else '100%')
  blocksize_override = (str(int(block_size.m_as(units.byte))) + 'B'
                        if block_size else None)

  for scenario in scenarios:
    # per legacy behavior, the block size parameter
    # overrides what is defined in the scenario string
    if blocksize_override:
      scenario['blocksize'] = blocksize_override

    # per legacy behavior, the size in the scenario_string overrides
    # size defined on the command line
    if 'size' not in scenario:
      scenario['size'] = default_size_string

  job_file_template = jinja2.Template(JOB_FILE_TEMPLATE,
                                      undefined=jinja2.StrictUndefined)

  # the template creates a cross product of all
  # (scenario X io_depths X num_jobs)
  # which allows a single run to produce all of these metrics
  return str(job_file_template.render(
      runtime=runtime,
      filename=filename,
      scenarios=scenarios,
      iodepths=io_depths,
      numjobs=num_jobs,
      direct=int(direct),
      parameters=parameters))


FILENAME_PARAM_REGEXP = re.compile('filename\s*=.*$', re.MULTILINE)


def ProcessedJobFileString(fio_jobfile_contents, remove_filename):
  """Modify the fio job if requested.

  Args:
    fio_jobfile_contents: the contents of a fio job file.
    remove_filename: bool. If true, remove the filename parameter from
      the job file.

  Returns:
    The job file as a string, possibly without filename parameters.
  """

  if remove_filename:
    return FILENAME_PARAM_REGEXP.sub('', fio_jobfile_contents)
  else:
    return fio_jobfile_contents


def GetOrGenerateJobFileString(job_file_path, scenario_strings,
                               against_device, disk, io_depths,
                               num_jobs, working_set_size, block_size,
                               runtime, direct, parameters, job_file_contents):
  """Get the contents of the fio job file we're working with.

  This will either read the user's job file, if given, or generate a
  new one.

  Args:
    job_file_path: string or None. The path to the user's job file, if
      provided.
    scenario_strings: list of strings or None. The workload scenarios
      to generate.
    against_device: bool. True if testing against a raw device, False
      if testing against a filesystem.
    disk: the disk.BaseDisk object to test against.
    io_depths: iterable of integers. The IO queue depths to test.
    num_jobs: iterable of integers. The number of fio processes to test.
    working_set_size: int or None. If int, the size of the working set
      in GB.
    block_size: Quantity or None. If Quantity, the block size to use.
    runtime: int. The number of seconds to run each job.
    direct: boolean. Whether to use direct IO.
    parameters: list. Other fio parameters to apply to all jobs.
    job_file_contents: string contents of fio job.

  Returns:
    A string containing a fio job file.
  """

  user_job_file_string = GetFileAsString(job_file_path)

  use_user_jobfile = job_file_path or not scenario_strings

  if use_user_jobfile:
    remove_filename = against_device
    return ProcessedJobFileString(user_job_file_string or job_file_contents,
                                  remove_filename)
  else:
    if against_device:
      filename = disk.GetDevicePath()
    else:
      # Since we pass --directory to fio, we must use relative file
      # paths or get an error.
      filename = DEFAULT_TEMP_FILE_NAME

    return GenerateJobFileString(filename, scenario_strings, io_depths,
                                 num_jobs, working_set_size, block_size,
                                 runtime, direct, parameters)


NEED_SIZE_MESSAGE = ('You must specify the working set size when using '
                     'generated scenarios with a filesystem.')


def WarnOnBadFlags():
  """Warn the user if they pass bad flag combinations."""

  if FLAGS.fio_jobfile:
    ignored_flags = {'--' + flag_name
                     for flag_name in FLAGS_IGNORED_FOR_CUSTOM_JOBFILE
                     if FLAGS[flag_name].present}

    if ignored_flags:
      logging.warning('Fio job file specified. Ignoring options "%s"',
                      ', '.join(ignored_flags))

  if (FLAGS.fio_jobfile is None and
      FLAGS.fio_generate_scenarios and
      not FLAGS.fio_working_set_size and
      not AgainstDevice()):
    logging.error(NEED_SIZE_MESSAGE)
    raise errors.Benchmarks.PrepareException(NEED_SIZE_MESSAGE)


def GetConfig(user_config):
  config = configs.LoadConfig(BENCHMARK_CONFIG, user_config, BENCHMARK_NAME)
  if FLAGS.fio_target_mode != AGAINST_FILE_WITHOUT_FILL_MODE:
    disk_spec = config['vm_groups']['default']['disk_spec']
    for cloud in disk_spec:
      disk_spec[cloud]['mount_point'] = None
  return config


def GetLogFlags(log_file_base):
  """Gets fio log files."""
  collect_logs = FLAGS.fio_lat_log or FLAGS.fio_bw_log or FLAGS.fio_iops_log
  fio_log_flags = [(FLAGS.fio_lat_log, '--write_lat_log=%(filename)s',),
                   (FLAGS.fio_bw_log, '--write_bw_log=%(filename)s',),
                   (FLAGS.fio_iops_log, '--write_iops_log=%(filename)s',),
                   (FLAGS.fio_hist_log, '--write_hist_log=%(filename)s',),
                   (collect_logs, '--log_avg_msec=%(interval)d',)]
  fio_command_flags = ' '.join([flag for given, flag in fio_log_flags if given])
  if FLAGS.fio_hist_log:
    fio_command_flags = ' '.join([
        fio_command_flags, '--log_hist_msec=%(hist_interval)d'])

  return fio_command_flags % {'filename': log_file_base,
                              'interval': FLAGS.fio_log_avg_msec,
                              'hist_interval': FLAGS.fio_log_hist_msec}


def CheckPrerequisites(benchmark_config):
  """Perform flag checks."""
  del benchmark_config  # unused
  WarnOnBadFlags()


def Prepare(benchmark_spec):
  exec_path = fio.GetFioExec()
  vms = benchmark_spec.vms
  vm_util.RunThreaded(lambda vm: PrepareWithExec(vm, exec_path), vms)


def GetFileAsString(file_path):
  if not file_path:
    return None
  with open(data.ResourcePath(file_path), 'r') as jobfile:
    return jobfile.read()


def PrepareWithExec(vm, exec_path):
  """Prepare the virtual machine to run FIO.

     This includes installing fio, bc, and libaio1 and pre-filling the
     attached disk. We also make sure the job file is always located
     at the same path on the local machine.

  Args:
    vm: The virtual machine to prepare the benchmark on.
    exec_path: string path to the fio executable

  """
  logging.info('FIO prepare on %s', vm)
  vm.Install('fio')

  # Choose a disk or file name and optionally fill it
  disk = vm.scratch_disks[0]

  if FillTarget():
    logging.info('Fill device %s on %s', disk.GetDevicePath(), vm)
    FillDevice(vm, disk, FLAGS.fio_fill_size, exec_path)

  # We only need to format and mount if the target mode is against
  # file with fill because 1) if we're running against the device, we
  # don't want it mounted and 2) if we're running against a file
  # without fill, it was never unmounted (see GetConfig()).
  if FLAGS.fio_target_mode == AGAINST_FILE_WITH_FILL_MODE:
    disk.mount_point = FLAGS.scratch_dir or MOUNT_POINT
    disk_spec = vm.disk_specs[0]
    vm.FormatDisk(disk.GetDevicePath(), disk_spec.disk_type)
    vm.MountDisk(disk.GetDevicePath(), disk.mount_point,
                 disk_spec.disk_type, disk.mount_options, disk.fstab_options)
  if FLAGS.fio_write_against_multiple_clients:
    vm.RemoteCommand('sudo rm -rf %s/%s' % (disk.mount_point, vm.name))
    vm.RemoteCommand('sudo mkdir -p %s/%s' % (disk.mount_point, vm.name))


def Run(benchmark_spec):
  """Spawn fio on vm(s) and gather results."""
  fio_exe = fio.GetFioExec()
  default_job_file_contents = GetFileAsString(data.ResourcePath('fio.job'))
  vms = benchmark_spec.vms
  samples = []

  path = REMOTE_JOB_FILE_PATH
  samples_list = vm_util.RunThreaded(
      lambda vm: RunWithExec(vm, fio_exe, path, default_job_file_contents), vms)
  for i, _ in enumerate(samples_list):
    for item in samples_list[i]:
      item.metadata['machine_instance'] = i
    samples.extend(samples_list[i])

  if FLAGS.fio_write_against_multiple_clients and samples:
    metrics = collections.defaultdict(list)
    for item in samples:
      # example metric: 'filestore-bandwidth:write:bandwidth'
      metrics[item.metric.split(':', 1)[-1]].append(item.value)

    samples.append(
        sample.Sample('Total_write_throughput', sum(metrics['write:bandwidth']),
                      'KB/s'))
    samples.append(
        sample.Sample('Total_write_iops', sum(metrics['write:iops']), 'ops'))
    earliest_start = min(metrics['start_time'])
    latest_start = max(metrics['start_time'])
    earliest_end = min(metrics['end_time'])
    latest_end = max(metrics['end_time'])
    # Invalid run if the start and end times don't overlap 95%.
    nonoverlap_percent = (latest_end - earliest_end + latest_start -
                          earliest_start) / (
                              earliest_end - latest_start)
    valid_run = (nonoverlap_percent < 0.05)
    for item in samples:
      item.metadata['valid_run'] = valid_run
      item.metadata['nonoverlap_percentage'] = nonoverlap_percent
  for item in samples:
    item.metadata['fio_target_mode'] = FLAGS.fio_target_mode
    item.metadata['fio_fill_size'] = FLAGS.fio_fill_size
    item.metadata['fio_rng'] = FLAGS.fio_rng

  return samples


def RunWithExec(vm, exec_path, remote_job_file_path, job_file_contents):
  """Spawn fio and gather the results.

  Args:
    vm: vm to run the benchmark on.
    exec_path: string path to the fio executable.
    remote_job_file_path: path, on the vm, to the location of the job file.
    job_file_contents: string contents of the fio job file.

  Returns:
    A list of sample.Sample objects.
  """
  logging.info('FIO running on %s', vm)

  disk = vm.scratch_disks[0]
  mount_point = disk.mount_point
  if FLAGS.fio_write_against_multiple_clients:
    mount_point = '%s/%s' % (disk.mount_point, vm.name)
    logging.info('FIO mount point changed to %s', mount_point)

  job_file_string = GetOrGenerateJobFileString(
      FLAGS.fio_jobfile,
      FLAGS.fio_generate_scenarios,
      AgainstDevice(),
      disk,
      FLAGS.fio_io_depths,
      FLAGS.fio_num_jobs,
      FLAGS.fio_working_set_size,
      FLAGS.fio_blocksize,
      FLAGS.fio_runtime,
      _DIRECT_IO.value,
      FLAGS.fio_parameters,
      job_file_contents)
  job_file_path = vm_util.PrependTempDir(vm.name + LOCAL_JOB_FILE_SUFFIX)
  with open(job_file_path, 'w') as job_file:
    job_file.write(job_file_string)
    logging.info('Wrote fio job file at %s', job_file_path)
    logging.info(job_file_string)

  vm.PushFile(job_file_path, remote_job_file_path)

  if AgainstDevice():
    fio_command = (f'{exec_path} --output-format=json '
                   f'--random_generator={FLAGS.fio_rng} '
                   f'--filename={disk.GetDevicePath()} {remote_job_file_path}')
  else:
    fio_command = (f'{exec_path} --output-format=json '
                   f'--random_generator={FLAGS.fio_rng} '
                   f'--directory={mount_point} {remote_job_file_path}')

  collect_logs = any([
      FLAGS.fio_lat_log, FLAGS.fio_bw_log, FLAGS.fio_iops_log,
      FLAGS.fio_hist_log
  ])

  log_file_base = ''
  if collect_logs:
    log_file_base = '%s_%s' % (PKB_FIO_LOG_FILE_NAME, str(time.time()))
    fio_command = ' '.join([fio_command, GetLogFlags(log_file_base)])

  # TODO(user): This only gives results at the end of a job run
  #      so the program pauses here with no feedback to the user.
  #      This is a pretty lousy experience.
  logging.info('FIO Results:')

  start_time = time.time()
  stdout, _ = vm.RobustRemoteCommand(
      fio_command, timeout=FLAGS.fio_command_timeout_sec)
  end_time = time.time()
  bin_vals = []
  if collect_logs:
    vm.PullFile(vm_util.GetTempDir(), '%s*.log' % log_file_base)
    if FLAGS.fio_hist_log:
      num_logs = int(vm.RemoteCommand(
          'ls %s_clat_hist.*.log | wc -l' % log_file_base)[0])
      bin_vals += [fio.ComputeHistogramBinVals(
          vm, '%s_clat_hist.%s.log' % (
              log_file_base, idx + 1)) for idx in range(num_logs)]
  samples = fio.ParseResults(job_file_string, json.loads(stdout),
                             log_file_base=log_file_base, bin_vals=bin_vals)

  samples.append(
      sample.Sample('start_time', start_time, 'sec', samples[0].metadata))
  samples.append(
      sample.Sample('end_time', end_time, 'sec', samples[0].metadata))

  return samples


def Cleanup(benchmark_spec):
  """Uninstall packages required for fio and remove benchmark files.

  Args:
    benchmark_spec: The benchmark specification. Contains all data that is
        required to run the benchmark.
  """
  vm = benchmark_spec.vms[0]
  logging.info('FIO Cleanup up on %s', vm)
  vm.RemoveFile(REMOTE_JOB_FILE_PATH)
  if not AgainstDevice() and not FLAGS.fio_jobfile:
    # If the user supplies their own job file, then they have to clean
    # up after themselves, because we don't know their temp file name.
    vm.RemoveFile(posixpath.join(vm.GetScratchDir(), DEFAULT_TEMP_FILE_NAME))
