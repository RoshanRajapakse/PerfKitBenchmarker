# Copyright 2014 Google Inc. All rights reserved.
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

import json
import logging
import posixpath
import re

import jinja2

from perfkitbenchmarker import flags
from perfkitbenchmarker import vm_util
from perfkitbenchmarker.packages import fio

LOCAL_JOB_FILE_NAME = 'fio.job'  # used with vm_util.PrependTempDir()
REMOTE_JOB_FILE_PATH = posixpath.join(vm_util.VM_TMP_DIR, 'fio.job')
DEFAULT_TEMP_FILE_NAME = 'fio-temp-file'
MAX_FILE_SIZE_GB = 100
DISK_USABLE_SPACE_FRACTION = 0.9


FLAGS = flags.FLAGS

flags.DEFINE_string('fio_jobfile', None,
                    'Job file that fio will use. Giving a custom job file '
                    'overrides the other fio options.')
flags.DEFINE_boolean('against_device', False,
                     'If true, test directly against the scratch disk.')
flags.DEFINE_string('device_fill_size', '100%',
                    'The amount of device to fill in prepare stage. '
                    'This flag is only valid when against_device=True. '
                    'The valid value can either be an integer, which '
                    'represents the number of bytes to fill or a '
                    'percentage, which represents the percentage '
                    'of the device.')
flags.DEFINE_string('io_depths', '1',
                    'IO queue depths to run on. Can specify a single number, '
                    'like --io_depths=1, or a range, like --io_depths=1-4')


FLAGS_IGNORED_FOR_CUSTOM_JOBFILE = {
    'against_device', 'device_fill_size', 'io_depth'}


IODEPTHS_REGEXP = re.compile(r'(\d+)(-(\d+))?')


def IODepthsValidator(string):
  match = IODEPTHS_REGEXP.match(string)
  return match and match.end() == len(string) and int(match.group(1)) > 0


flags.RegisterValidator('io_depths',
                        IODepthsValidator,
                        message='--io_depths must be an integer '
                                'or a range of integers, all > 0')


BENCHMARK_INFO = {'name': 'fio',
                  'description': 'Runs fio in sequential, random, read '
                                 'and write modes.',
                  'scratch_disk': True,
                  'num_machines': 1}


JOB_FILE_TEMPLATE = """
[global]
ioengine=libaio
invalidate=1
blocksize=4k
direct=1
runtime=10m
time_based
filename={{filename}}
do_verify=0
verify_fatal=0
randrepeat=0
size={{size}}

{% for rwkind in ('randread', 'randwrite') %}
{% for iodepth in iodepths %}
[{{rwkind}}-io-depth-{{iodepth}}]
stonewall
rw={{rwkind}}
iodepth={{iodepth}}

{% endfor %}
{% endfor %}
"""


def GetIODepths(io_depths):
  """Parse the io_depths parameter.

  Args:
    io_depths: a string in the format of the --io_depths flag.

  Returns:
    An iterable of integers.

  Raises:
    ValueError if FLAGS.io_depths doesn't follow a format it recognizes.
  """

  match = IODEPTHS_REGEXP.match(io_depths)

  if match.group(2) is None:
    return [int(match.group(1))]
  else:
    return range(int(match.group(1)), int(match.group(3)) + 1)


def GenerateJobFileString(mount_point, against_device,
                          device_fill_size, io_depths, device_size):
  """Write a fio job file.

  Args:
    mount_point: the mount point of the disk we're testing against.
    against_device: bool. True if we're using a raw disk.
    device_fill_size: string. The amount of the disk to pre-fill.
    io_depths: string. The contents of the io_depths flag.
    device_size: int. The size of the device, in gigabytes.

  Returns:
    The contents of a fio job file, as a string.
  """

  if against_device:
    filename = mount_point
    size = device_fill_size
  else:
    filename = posixpath.join(mount_point, DEFAULT_TEMP_FILE_NAME)
    size = str(min(MAX_FILE_SIZE_GB,
                   int(DISK_USABLE_SPACE_FRACTION * device_size))) + 'G'

  return str(jinja2.Template(JOB_FILE_TEMPLATE,
                             undefined=jinja2.StrictUndefined).render(
      filename=filename,
      size=size,
      iodepths=GetIODepths(io_depths)))


def JobFileString(fio_jobfile, mount_point, against_device,
                  device_fill_size, io_depths, device_size):
  """Get the contents of our job file.

  Args:
    fio_jobfile: string or False. The path to the user's jobfile, if provided.
    mount_point: the mount point of the disk we're testing against.
    against_device: bool. True if we're using a raw disk.
    device_fill_size: string. The amount of the disk to pre-fill.
    io_depths: string. The contents of the io_depths flag.
    device_size: int. The size of the device, in gigabytes.

    vm: the virtual_machine.BaseVirtualMachine that we will run on.

  Returns:
    A string containing the user's job file.
  """

  if fio_jobfile:
    with open(fio_jobfile, 'r') as jobfile:
      return jobfile.read()
  else:
    return GenerateJobFileString(mount_point, against_device,
                                 device_fill_size, io_depths, device_size)


def GetInfo():
  return BENCHMARK_INFO


def Prepare(benchmark_spec):
  """Prepare the virtual machine to run FIO.

     This includes installing fio, bc, and libaio1 and insuring that
     the attached disk is large enough to support the fio
     benchmark. We also make sure the job file is always located at
     the same path on the local machine.

  Args:
    benchmark_spec: The benchmark specification. Contains all data that is
        required to run the benchmark.

  """


  if FLAGS.fio_jobfile:
    ignored_flags = {flag_name for flag_name in FLAGS_IGNORED_FOR_CUSTOM_JOBFILE
                     if FLAGS[flag_name].present}

    if ignored_flags:
      logging.warning('Fio job file specified. Ignoring options "%s"',
                      ', '.join(ignored_flags))
  if FLAGS['device_fill_size'].present and not FLAGS['against_device'].present:
    logging.warning('--device_fill_size has no effect without --against_device')

  vm = benchmark_spec.vms[0]
  logging.info('FIO prepare on %s', vm)
  vm.Install('fio')

  disk = vm.scratch_disks[0]
  if FLAGS.against_device and not FLAGS.fio_jobfile:
    mount_point = disk.mount_point
    logging.info('Umount scratch disk on %s at %s', vm, mount_point)
    vm.RemoteCommand('sudo umount %s' % mount_point)

    device_path = disk.GetDevicePath()
    logging.info('Fill scratch disk on %s at %s', vm, device_path)
    command = (
        ('sudo %s --filename=%s --ioengine=libaio '
         '--name=fill-device --blocksize=512k --iodepth=64 '
         '--rw=write --direct=1 --size=%s') %
        (fio.FIO_PATH, device_path, FLAGS.device_fill_size))
    vm.RemoteCommand(command)

  job_file_path = vm_util.PrependTempDir(LOCAL_JOB_FILE_NAME)
  with open(job_file_path, 'w') as job_file:
    job_file.write(JobFileString(FLAGS.fio_jobfile,
                                 disk.mount_point,
                                 FLAGS.against_device,
                                 FLAGS.device_fill_size,
                                 FLAGS.io_depths,
                                 disk.disk_size))
    logging.info('Wrote fio job file at %s', job_file_path)

  vm.PushFile(job_file_path, REMOTE_JOB_FILE_PATH)


def Run(benchmark_spec):
  """Spawn fio and gather the results.

  Args:
    benchmark_spec: The benchmark specification. Contains all data that is
        required to run the benchmark.

  Returns:
    A list of samples in the form of 3 or 4 tuples. The tuples contain
        the sample metric (string), value (float), and unit (string).
        If a 4th element is included, it is a dictionary of sample
        metadata.
  """
  vm = benchmark_spec.vms[0]
  logging.info('FIO running on %s', vm)

  fio_command = 'sudo %s --output-format=json %s' % (fio.FIO_PATH,
                                                     REMOTE_JOB_FILE_PATH)
  # TODO(user): This only gives results at the end of a job run
  #      so the program pauses here with no feedback to the user.
  #      This is a pretty lousy experience.
  logging.info('FIO Results:')
  stdout, stderr = vm.RemoteCommand(fio_command, should_log=True)

  disk = vm.scratch_disks[0]
  return fio.ParseResults(JobFileString(FLAGS.fio_jobfile,
                                        disk.mount_point,
                                        FLAGS.against_device,
                                        FLAGS.device_fill_size,
                                        FLAGS.io_depths,
                                        disk.disk_size),
                          json.loads(stdout))


def Cleanup(benchmark_spec):
  """Uninstall packages required for fio and remove benchmark files.

  Args:
    benchmark_spec: The benchmark specification. Contains all data that is
        required to run the benchmark.
  """
  vm = benchmark_spec.vms[0]
  logging.info('FIO Cleanup up on %s', vm)
  vm.RemoveFile(REMOTE_JOB_FILE_PATH)
  if not FLAGS.against_device and not FLAGS.fio_jobfile:
    # If the user supplies their own job file, then they have to clean
    # up after themselves, because we don't know their temp file name.
    vm.RemoveFile(posixpath.join(vm.GetScratchDir(), DEFAULT_TEMP_FILE_NAME))
