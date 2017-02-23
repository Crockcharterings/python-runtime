#!/usr/bin/env python3

# Copyright 2017 Google Inc. All Rights Reserved.
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

"""Emulate the Google Container Builder locally.

The input is a local cloudbuild.yaml file.  This is translated into a
series of commands for the locally installed Docker daemon.  These
commands are output as a shell script and optionally executed.

The output images are not pushed to the Google Container Registry.
Not all cloudbuild.yaml functionality is supported.

See https://cloud.google.com/container-builder/docs/api/build-steps
for more information.
"""

import argparse
import collections
import functools
import io
import os
import re
import shlex
import subprocess
import sys

import yaml

import validation_utils

# Exclude non-printable control characters (including newlines)
PRINTABLE_REGEX = re.compile(r"""^[^\x00-\x1f]*$""")

# File template
BUILD_SCRIPT_HEADER = """\
#!/bin/bash
# This is a generated file.  Do not edit.

set -euo pipefail

SOURCE_DIR=.

# Setup staging directory
HOST_WORKSPACE=$(mktemp -d)
function cleanup {
    if [ "${HOST_WORKSPACE}" != '/' -a -d "${HOST_WORKSPACE}" ]; then
        rm -rf "${HOST_WORKSPACE}"
    fi
}
trap cleanup EXIT

# Copy source to staging directory
echo "Copying source to staging directory ${HOST_WORKSPACE}"
rsync -avzq --exclude=.git "${SOURCE_DIR}" "${HOST_WORKSPACE}"

# Build commands
"""

BUILD_SCRIPT_FOOTER = """\
# End of build commands

echo "Build completed successfully"
"""


# Validated cloudbuild recipe + flags
CloudBuild = collections.namedtuple('CloudBuild', 'output_script run steps')

# Single validated step in a cloudbuild recipe
Step = collections.namedtuple('Step', 'args dir_ env name')


def get_cloudbuild(raw_config, args):
    """Read and validate a cloudbuild recipe

    Args:
        raw_config (dict): deserialized cloudbuild.yaml
        args (argparse.Namespace): ccommand line flags

    Returns:
        CloudBuild: valid configuration
    """
    if not isinstance(raw_config, dict):
        raise ValueError(
            'Expected {} contents to be of type "dict", but found type "{}"'.
            format(args.config, type(raw_config)))

    raw_steps = validation_utils.get_field_value(raw_config, 'steps', list)
    if not raw_steps:
        raise ValueError('No steps defined in {}'.format(args.config))

    steps = [get_step(raw_step) for raw_step in raw_steps]
    return CloudBuild(
        output_script=args.output_script,
        run=args.run,
        steps=steps,
    )


def get_step(raw_step):
    """Read and validate a single cloudbuild step

    Args:
        raw_step (dict): deserialized step

    Returns:
        Step: valid build step
    """
    if not isinstance(raw_step, dict):
        raise ValueError(
            'Expected step to be of type "dict", but found type "{}"'.
            format(type(raw_step)))
    raw_args = validation_utils.get_field_value(raw_step, 'args', list)
    args = [validation_utils.get_field_value(raw_args, index, str)
            for index in range(len(raw_args))]
    dir_ = validation_utils.get_field_value(raw_step, 'dir', str)
    raw_env = validation_utils.get_field_value(raw_step, 'env', list)
    env = [validation_utils.get_field_value(raw_env, index, str)
           for index in range(len(raw_env))]
    name = validation_utils.get_field_value(raw_step, 'name', str)
    return Step(
        args=args,
        dir_=dir_,
        env=env,
        name=name,
    )


def generate_command(step):
    """Generate a single shell command to run for a single cloudbuild step

    Args:
        step (Step): Valid build step

    Returns:
        [str]: A single shell command, expressed as a list of quoted tokens.
    """
    quoted_args = [shlex.quote(arg) for arg in step.args]
    quoted_env = []
    for env in step.env:
        quoted_env.extend(['--env', shlex.quote(env)])
    quoted_name = shlex.quote(step.name)
    workdir = '/workspace'
    if step.dir_:
        workdir = os.path.join(workdir, shlex.quote(step.dir_))
    process_args = [
        'docker',
        'run',
        '--volume',
        '/var/run/docker.sock:/var/run/docker.sock',
        '--volume',
        '/root/.docker:/root/.docker',
        '--volume',
        '${HOST_WORKSPACE}:/workspace',
        '--workdir',
        workdir,
    ] + quoted_env + [quoted_name] + quoted_args
    return process_args


def generate_script(cloudbuild):
    """Generate the contents of a shell script

    Args:
        cloudbuild (CloudBuild): Valid cloudbuild configuration

    Returns:
        (str): Contents of shell script
    """
    outfile = io.StringIO()
    outfile.write(BUILD_SCRIPT_HEADER)
    docker_commands = [generate_command(step) for step in cloudbuild.steps]
    for docker_command in docker_commands:
        line = ' '.join(docker_command) + '\n\n'
        outfile.write(line)
    outfile.write(BUILD_SCRIPT_FOOTER)
    s = outfile.getvalue()
    outfile.close()
    return s


def make_executable(path):
    """Set executable bit(s) on file"""
    # http://stackoverflow.com/questions/12791997
    mode = os.stat(path).st_mode
    mode |= (mode & 0o444) >> 2  # copy R bits to X
    os.chmod(path, mode)


def write_script(cloudbuild, contents):
    """Write a shell script to a file."""
    print('Writing build script to {}'.format(cloudbuild.output_script))
    with open(cloudbuild.output_script, 'w', encoding='utf8') as outfile:
        outfile.write(contents)
    make_executable(cloudbuild.output_script)


def local_cloudbuild(args):
    """Execute the steps of a cloudbuild.yaml locally

    Args:
        args: command line flags as per parse_args
    """
    # Load and parse cloudbuild.yaml
    with open(args.config, 'r', encoding='utf8') as cloudbuild_file:
        raw_config = yaml.safe_load(cloudbuild_file)

    # Determine configuration
    cloudbuild = get_cloudbuild(raw_config, args)

    # Create shell script
    contents = generate_script(cloudbuild)
    write_script(cloudbuild, contents)

    # Run shell script
    if cloudbuild.run:
        args = [os.path.abspath(cloudbuild.output_script)]
        subprocess.check_call(args)


def parse_args(argv):
    """Parse and validate command line flags"""
    parser = argparse.ArgumentParser(
        description='Process cloudbuild.yaml locally to build Docker images')
    parser.add_argument(
        '--config',
        type=functools.partial(
            validation_utils.validate_arg_regex, flag_regex=PRINTABLE_REGEX),
        default='cloudbuild.yaml',
        help='Path to cloudbuild.yaml file'
    )
    parser.add_argument(
        '--output_script',
        type=functools.partial(
            validation_utils.validate_arg_regex, flag_regex=PRINTABLE_REGEX),
        help='Filename to write shell script to',
    )
    parser.add_argument(
        '--no-run',
        action='store_false',
        help='Create shell script but don\'t execute it',
        dest='run',
    )
    args = parser.parse_args(argv[1:])
    if not args.output_script:
        args.output_script = args.config + "_local.sh"
    return args


def main():
    args = parse_args(sys.argv)
    local_cloudbuild(args)


if __name__ == '__main__':
    main()
