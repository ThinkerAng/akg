#!/usr/bin/env python3
# coding: utf-8
# Copyright 2019 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""util"""
import sys
import gc
import inspect
import datetime
import os
import uuid
import logging
import time
import random
import subprocess
import re
from timeit import default_timer as timer
from threading import Thread
from functools import reduce
import numpy as np

import akg
from akg.backend import aic_model
from akg.build_module import help_tiling_level
from akg import backend as cce
import akg.tvm
from akg.tvm import rpc
from akg.utils import result_analysis as ra_util
from akg.utils import format_transform as ft_util
from akg.utils import custom_tiling as ct_util
from akg.utils import validation_check as vc_util
from akg.utils.dsl_create import TensorUtils
from akg.utils import dump_cuda_meta

sh = logging.StreamHandler(sys.stdout)
logging.getLogger().addHandler(sh)
logging.getLogger().setLevel(logging.INFO)

rpc_machine = {}
rpc_lb = {}
PERFORMANCE_TEST_FILE = "PERFORMANCE_TEST_FILE"
BINDS = "binds"
RANDOM_SEED_NUM = 20
PROF_ERROR_CODE = 9999999999


def func_time_required(func_name):
    """Checking the Time Required for Function Running."""
    def wrapper(*args, **kwargs):
        t0 = time.time()
        result = func_name(*args, **kwargs)
        t1 = time.time()
        logging.info("func_time_required func:%s, running:%lf seconds", func_name.__name__, (t1 - t0))
        return result
    return wrapper


def create_code(kernel_name, code_path=None, code=None, code_type="CCE"):
    """
    Create cce or cuda file.

    Args:
        kernel_name: file name.
        code_path: file path.
        code: code.
        code_type: code type.
    """
    if code_type == "CCE":
        postfix = ".cce"
    elif code_type == "CUDA":
        postfix = ".cu"
    else:
        logging.info("the target code type %s is not supported.", code_type)

    if not code_path:
        code_path = "./"
    
    if code_type == "CCE" and len(code_path) > 4 and code_path[-4:].lower() == postfix:
        real_path = code_path
    elif code_type == "CUDA" and len(code_path) > 3 and code_path[-3:].lower() == postfix:
        real_path = code_path
    else:
        if code_path[-1] == r"/":
            real_path = code_path + kernel_name + postfix
        else:
            real_path = code_path + r"/" + kernel_name + postfix
    dir_path = r"/".join(real_path.split(r"/")[:-1])
    if not os.path.isdir(dir_path):
        os.makedirs(dir_path)
    
    with open(real_path, 'wt') as ss:
        ss.write(code)



def gen_name_kernel(kernel, dtype, shapes):
    """generate kernel name."""
    def _flat_array(srclist, dstlist):
        for i in srclist:
            if isinstance(i, (list, tuple)):
                _flat_array(i, dstlist)
            else:
                dstlist.append(i)
    res = ''
    flat = []
    _flat_array(shapes, flat)
    for s in flat:
        res = "%s%s'_'" % (res, s)
    res = "%s_%s%s" % (kernel, res, dtype)
    return res


def load_rpc_server_info(mode):
    """
    load rpc server host and port info.

    Args:
        mode (str): string of runtime choose, can set ca aic and rpc.
    """
    env_dic = os.environ
    if env_dic.get('RPC_HOST') and env_dic.get('RPC_PORT'):
        return None

    if mode == 'rpc_cloud':
        logging.error("runtime_mode=rpc_cloud must set 1980 host ip and port!")
        raise Exception("ERROR:runtime_mode=rpc_cloud must set 1980 host ip and port!")

    rpc_server_info_config = env_dic.get('RPC_SERVER_INFO_FILE')
    if not rpc_server_info_config:
        logging.error("runtime_mode=rpc must set RPC_SERVER_INFO_FILE for rpc server info config")
        raise Exception("ERROR:runtime_mode=rpc must set RPC_SERVER_INFO_FILE for rpc server info config")

    # load rpc server host and port info from local file.
    import json
    with open(rpc_server_info_config, 'r') as f:
        info = json.load(f)

    for i in info:
        rpc_machine[i] = info[i]
        rpc_lb[i] = 0.0
    return None


def dispatch(rank=0):
    """Function for lock waiting dispatch handle version 1."""
    def _sort_by_value(d):
        items = list(d.items())
        random.shuffle(items)
        items.sort(key=lambda x: x[1])
        return [item[0] for item in items]
    for k, v in rpc_lb.items():
        logging.info("######rpc_lb[%s]=%f", rpc_machine[k][0], v)
    lb_list = _sort_by_value(rpc_lb)
    if len(lb_list) > rank:
        return lb_list[rank]
    return lb_list[len(lb_list) - 1]


def commit(remote, weight):
    rpc_lb[remote] = weight


@func_time_required
def mod_launch_rpc_worker(mod, args, outputs, host, port, tuning=False):
    """internal RPC worker, should be called by mod_launch_rpc_thread."""
    logging.info("%s:====start connect to rpc ip: %s, rpc port: %d ",
                 datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), host, port)
    remote = rpc.connect(host, port, session_timeout=300)
    logging.info("%s:====connect to rpc ip: %s, rpc port: %d finished ",
                 datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), host, port)
    uuid_str = uuid.uuid4().hex
    temp_file_name = "stackvm_%s.o" % uuid_str
    mod.save(temp_file_name)
    remote.upload(temp_file_name)
    remote_mod = remote.load_module(temp_file_name)
    ctx = remote.cce()
    arg_list = []
    for a in args:
        arg_list.append(akg.tvm.nd.array(a, ctx))
    start_time = timer()
    remote_mod(*arg_list)
    ctx.sync()
    if os.path.exists(temp_file_name):
        os.remove(temp_file_name)
    out_list = []
    for i in outputs:
        out = arg_list[len(arg_list) + i if i < 0 else i].asnumpy()
        out_list.append(out)
    # this time measure is no accurate now, to be improved soon
    t = timer() - start_time
    if not tuning:
        return out_list[0] if len(out_list) == 1 else tuple(out_list)
    stat_info = {"run_time": t}
    return out_list[0] if len(out_list) == 1 else tuple(out_list), stat_info


def mod_launch_rpc_thread(mode, mod, args, outputs, results, need_retry, retry, tuning=False):
    """internal RPC thread, should be called by mod_launch_rpc_multithread."""
    remoteevb = '0'
    host = None
    port = None
    env_dic = os.environ
    if env_dic.get('RPC_HOST') and env_dic.get('RPC_PORT'):
        host = env_dic.get('RPC_HOST')
        port = int(env_dic.get('RPC_PORT'))
    else:
        if mode == 'rpc_cloud':
            logging.error("runtime_mode=rpc_cloud must set 1980 host ip and port!")
            raise Exception("ERROR:runtime_mode=rpc_cloud must set 1980 host ip and port!")
        remoteevb = dispatch(retry)
        host = rpc_machine[remoteevb][0]
        port = rpc_machine[remoteevb][1]

    start_time = timer()
    end_time = 0.0
    logging.debug("rpc ip: %s, rpc port: %d", host, port)
    try:
        out_list = mod_launch_rpc_worker(mod, args, outputs, host, port, tuning=tuning)
        end_time = timer()
        t = end_time - start_time
        if not env_dic.get('RPC_HOST'):
            commit(remoteevb, 20 if t > 20 else t)
        logging.info("===this round host is %s time is %f", host, (end_time - start_time))
        results[retry] = out_list
    except RuntimeError:
        need_retry[retry] = True
        end_time = timer()
        logging.error("===Failed! this round host is %s time is %f", host, (end_time - start_time))
        if not env_dic.get('RPC_HOST'):
            commit(remoteevb, end_time - start_time + 20 * (retry + 1))
        logging.error("rpc retry error: %d %s", retry, sys.exc_info())


def mod_launch_rpc(mode, mod, args, outputs, tuning=False):
    """
    launch rpc or rpc_cloud module with retry.

    Note:
        To minimize waiting time of struggler RPC servers, we wait for a short timeout and spawn
        a new thread after the timeout.
        In normal case, RPC would complete before the short timeout, so, only one thread will be created.
        When the RPC server is slow, we create multiple threads that run concurrently.
        We wait for the first thread that successfully completes its work and return the result.
        If a thread fails (an exception is raised), we spawn a new thread to retry.
        Newly spawned threads will use different RPC servers.
        We bound the maximum number of threads, i.e. maximum number of retries.
    """
    max_num_threads = 5

    import operator
    arg_filter = filter(lambda x: isinstance(x, np.ndarray), args)
    arg_tensor = list(arg_filter)
    tensor_size = reduce(operator.add, [reduce(operator.mul, arg.shape) for arg in arg_tensor])
    expected_upload_speed = 5e6
    expected_upload_time = int(tensor_size / expected_upload_speed)

    timeout_before_spawning_new_thread = 200 + expected_upload_time
    poll_interval = 1
    thread_timeout = 400 + expected_upload_time * 3

    load_rpc_server_info(mode)

    threads = [None] * max_num_threads
    results = [None] * max_num_threads
    need_retry = [None] * max_num_threads
    retried = [False] * max_num_threads
    for thread_index in range(max_num_threads):
        if thread_index > 0:
            logging.error("Thread %d run for %d seconds, spawn a new thread to retry",
                          (thread_index - 1), timeout_before_spawning_new_thread)
        threads[thread_index] = Thread(target=mod_launch_rpc_thread,
                                       args=(mode, mod, args, outputs, results, need_retry, thread_index, tuning))
        # daemonize the thread to prevent long running threads from hanging the whole process
        threads[thread_index].daemon = True
        threads[thread_index].start()
        poll_count = timeout_before_spawning_new_thread // poll_interval
        while poll_count > 0:
            poll_count -= 1
            # wait for the newly created thread, because it is most likely to complete first
            threads[thread_index].join(poll_interval)
            for poll_index in range(thread_index + 1):
                if not threads[poll_index].is_alive() and not need_retry[poll_index]:
                    return results[poll_index]
                if need_retry[poll_index] and not retried[poll_index]:
                    logging.error("Thread %d exit with error, spawn a new thread immediately", poll_index)
                    poll_count = 0
                    retried[poll_index] = True

    logging.error("All %d threads are created, poll the threads until the first one exits normally, \
                  or all threads exit abnormally or timeout", max_num_threads)
    poll_count = thread_timeout // poll_interval
    for _ in range(poll_count):
        threads[max_num_threads - 1].join(poll_interval)
        exit_thread_count = 0
        for poll_index in range(max_num_threads):
            if not threads[poll_index].is_alive() and not need_retry[poll_index]:
                return results[poll_index]
            if not threads[poll_index].is_alive():
                exit_thread_count += 1
            if exit_thread_count == max_num_threads:
                logging.error("All %d threads exit abnormally", max_num_threads)
                return None

    logging.error("All %d threads timeout", max_num_threads)
    return None


def profiling_mode_run(mod, args, outputs, tuning, device_id):
    """
    Function for collecting cycle data from device.

    Args:
        mod: CCE Module.
        args: list or tuple of numpy array.
        outputs: list or tuple of output argment index.
        tuning: tuning model.
        device_id: device_id on device.
    """
    ctx = akg.tvm.ndarray.cce(device_id)
    arg_list = []
    for a in args:
        arg_list.append(akg.tvm.nd.array(a, ctx))
    mod(*arg_list)
    ctx.sync()
    out_list = []
    cycle = profiling_analyse(device_id)
    for i in outputs:
        out = arg_list[len(arg_list) + i if i < 0 else i].asnumpy()
        out_list.append(out)
    logging.info('=====parsing cycles==============================')
    if cycle != PROF_ERROR_CODE:
        logging.info(cycle)
    else:
        logging.error("OOPS, can't correctly parsing cycles!")
    TestUtils.record_cycle(cycle)
    logging.info('=====parsing cycles==============================')
    if tuning:
        return out_list[0] if len(out_list) == 1 else tuple(out_list), {'run_time': cycle}
    return out_list[0] if len(out_list) == 1 else tuple(out_list)


def profiling_analyse(device_id):
    """analyse profiling."""

    def exec_cmds_with_pipe(cmd_list):
        cmd_num = len(cmd_list)
        if cmd_num <= 1:
            raise RuntimeError("length of cmd_list should be greater than 1.")
        ps = []
        for i, cmd in enumerate(cmd_list):
            if i == 0:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
            else:
                p = subprocess.Popen(cmd, stdin=ps[-1].stdout, stdout=subprocess.PIPE)
            ps.append(p)
        for p in ps:
            p.wait()
        return ps[-1].communicate()

    if not isinstance(device_id, int):
        raise TypeError("device_id must be an integer.")

    try:
        public_path = "/var/log/npu/profiling"
        cmd_list = [
            ["find", public_path, "-iname", "*.log.%d" % device_id, "-printf", "'%T+\t%p\n'"],
            ["grep", "JOB"],
            ["sort", "-r"],
            ["head", "-n10"],
            ["awk", "{print $2}"],
            ["head", "-n1"],
        ]
        p = exec_cmds_with_pipe(cmd_list)
        for _ in range(5):
            if p[0].decode('utf8').strip() == '':
                time.sleep(1)
        try:
            job_file = p[0].decode('utf8').strip().split('/')[-2]
        except BaseException:
            logging.warning("failed to decode profiling result")
            return None
        logging.debug("job file is: %s", job_file)
        from akg.backend import parsing_profiling_data
        return parsing_profiling_data.parsing(public_path + '/' + job_file)
    except SyntaxError as e:
        logging.error(e)
        return PROF_ERROR_CODE

def mod_launch_air(mod, args, outputs):
    """launch mod on kc_air."""
    ctx = akg.tvm.ndarray.cce(0)
    arg_list = []
    for a in args:
        if isinstance(a, np.ndarray):
            arg_list.append(akg.tvm.nd.array(a, ctx))
        elif isinstance(a, (list, tuple)):
            for aa in a:
                if isinstance(aa, np.ndarray):
                    arg_list.append(akg.tvm.nd.array(aa, ctx))
                else:
                    arg_list.append(aa)
        else:
            arg_list.append(a)
    for retry in range(3):
        need_retry = False
        try:
            mod(*arg_list)
            ctx.sync()
            out_list = []
            if not need_retry:
                for i in outputs:
                    out = arg_list[len(arg_list) + i if i < 0 else i].asnumpy()
                    out_list.append(out)
                return out_list[0] if len(out_list) == 1 else tuple(out_list)
        except RuntimeError:
            need_retry = True
            logging.error("kc_air retry error: %d %s", retry, sys.exc_info())
    logging.error("kc_air runtime error, please check!")
    return None

@func_time_required
def mod_launch(mod, args, outputs=(-1,), tuning=False, device_id=0, expect=None):
    """
    unified run CCE kernel api.

    Args:
        mod (str): CCE Module, string of runtime choose, can set ca aic and rpc.
        args (Union[list, tuple]): list or tuple of numpy array.
        outputs (Union[list, tuple]): list or tuple of output argment index.
        tuning (bool): tuning model.
        device_id: device_id on device.
        expect: when mode in ["compile_cloud", "compile_mini"], return it.

    Returns:
        output numpy array, or tuple of numpy array if multi-output.
    """

    gc.collect()
    if mod.imported_modules[0].type_key == 'cuda':
        ctx = akg.tvm.context('cuda', device_id)
        mod_args = [akg.tvm.nd.array(a, ctx) for a in args]
        mod(*mod_args)
        out_list = [mod_args[len(args) + i if i < 0 else i].asnumpy() for i in outputs]
        return out_list[0] if len(out_list) == 1 else tuple(out_list)

    stat_info = {}
    profiling_mode = get_profiling_mode()
    if profiling_mode:
        return profiling_mode_run(mod, args, outputs, tuning, device_id)
    mode = get_runtime_mode()
    if mode == 'aic':
        output = aic_model.launch(mod, args, outputs)
        if not tuning:
            return output
        ra_util.get_ticks(stat_info)
        return output, stat_info
    if mode == 'aic_cloud':
        output = aic_model.launch(mod, args, outputs, spec=aic_model.Spec.CLOUD)
        if not tuning:
            return output
        ra_util.get_ticks(stat_info)
        return output, stat_info
    if mode in ('rpc', 'rpc_cloud'):
        return mod_launch_rpc(mode, mod, args, outputs, tuning)
    if mode in ('ca', 'air', 'air_cloud'):
        return mod_launch_air(mod, args, outputs)
    if mode in ("compile_cloud", "compile_mini"):
        return expect
    if mode in ("csim", "ccesim", "cdiff"):
        from akg.backend.csim import csim_launch
        return csim_launch(args, outputs)
    if mode == "cpu":
        tvm_array = []
        ctx = akg.tvm.context("llvm", 0)
        for _, args_val in enumerate(args):
            tvm_temp = akg.tvm.nd.array(args_val, ctx)
            tvm_array.append(tvm_temp)
        mod(*tvm_array)
        return tvm_array[-1].asnumpy()

    raise ValueError("mode must be aic, rpc, aic_cloud, ca, compile_cloud, compile_mini, cpu, csim, ccesim or cdiff")


def gen_kernel_name(input_shapes, input_types, op_attrs=None, kernel_name=""):
    """generate kernel name."""
    dir_max_length = 250
    shape_info = ''
    for _, (shape, dtype) in enumerate(zip(input_shapes, input_types)):
        if isinstance(shape, (list, tuple)) and shape and isinstance(shape[0], (list, tuple)):
            for _, tmp_shape in enumerate(shape):
                vc_util.check_shape(tmp_shape)
                tmp_shape = list(tmp_shape)
                str_tmp_shape = [str(tmp) for tmp in tmp_shape]
                shape_info = "%s_%s_%s" % (shape_info, dtype, '_'.join(str_tmp_shape))
        elif isinstance(shape, akg.tvm.tensor.Tensor):
            for tmp_shape in shape.shape:
                if isinstance(tmp_shape, akg.tvm.expr.Var):
                    str_shape = tmp_shape.name
                else:
                    str_shape = str(tmp_shape)
                shape_info = "%s_%s_%s" % (shape_info, dtype, '_'.join(str_shape))
        else:
            vc_util.check_shape(shape)
            if isinstance(shape, akg.tvm.expr.Var):
                shape = [shape]
            shape = list(shape)
            str_shape = [str(i) for i in shape]
            shape_info = "%s_%s_%s" % (shape_info, dtype, '_'.join(str_shape))

    if op_attrs is not None:
        for tmp in op_attrs:
            if isinstance(tmp, (list, tuple)):
                for ele in tmp:
                    if isinstance(ele, (list, tuple)):

                        str_tmp = [str(i) for i in ele]
                        shape_info = shape_info + '_' + '_'.join(str_tmp)
                    else:
                        shape_info = shape_info + '_' + str(ele)

            elif isinstance(tmp, (int, float)):
                shape_info = shape_info + '_' + str(tmp)

            elif isinstance(tmp, (str)):
                shape_info = shape_info + '_' + tmp

            elif isinstance(tmp, (np.ndarray)):
                shape = list(tmp.shape)
                str_shape = [str(i) for i in shape]
                shape_info = shape_info + '_' + '_'.join(str_shape)

    kernel_name = kernel_name + shape_info
    kernel_name = re.sub(r'[^0-9a-zA-Z]+', '_', kernel_name)
    if len(kernel_name) > dir_max_length:
        logging.info("Dir name %s exceed maximal length, use first %d char as dir name.", kernel_name, dir_max_length)
        kernel_name = kernel_name[:dir_max_length]
    return kernel_name


@func_time_required
def op_build_test(op_func, input_shapes, input_types, op_attrs=None, kernel_name="",
                  attrs=None, log_cce=False, dump_ir=True, dump_code=True,
                  polyhedral=True, tuning=False):
    """
    Return module from op_build with given inputs, distinguish tuning mode.

    Args:
        op_func (function returning an op or (op, [op_vars])): The op build function
        input_shapes(iterable of iterable of int): the dim sizes for input for op
        input_types (iterable of iterable of str): the dtypes for each input
        op_attrs (list or tuple): extra attributes for the op.
        kernel_name (str): name of op.
        attrs (dict): tiling parameter.
        log_cce (bool): False by default.
        dump_ir (bool): True by default.
        dump_code (bool): False by default.
        polyhedral (bool): True by default.
        tuning (bool): False by default.

    Return:
        module.
    """
    if isinstance(attrs, dict) and 'tuning' in attrs.keys():
        kernel_name = kernel_name
    else:
        kernel_name = gen_kernel_name(input_shapes, input_types, op_attrs, kernel_name)
    logging.debug('kernel_name---------- %s', str(kernel_name))
    mod = op_build(op_func, input_shapes, input_types, op_attrs, kernel_name,
                   attrs, log_cce, dump_ir, dump_code,
                   polyhedral, tuning)
    return mod


def recursive_copy(obj):
    """
    Copy a container object recursively

    Args:
        obj (list, tuple, dict or object): input container object.

    Return:
        copied object.
    """
    if isinstance(obj, list):
        return [recursive_copy(it) for it in obj]
    if isinstance(obj, tuple):
        return tuple([recursive_copy(it) for it in obj])
    if isinstance(obj, dict):
        copy_obj = dict()
        for key in obj:
            copy_obj[key] = recursive_copy(obj[key])
        return copy_obj
    return obj


def op_build(op_func, input_shapes, input_types, op_attrs=None, kernel_name="",
             attrs=None, log_cce=False, dump_ir=True, dump_code=True,
             polyhedral=True, tuning=False):
    """
    Return module built from op_func with given inputs.

    Args:
        op_func (function returning an op or (op, [op_vars])): The op build function.
        input_shapes(iterable of iterable of int): the dim sizes for input for op.
        input_types (iterable of iterable of str): the dtypes for each input.
        op_attrs (list or tuple): extra attributes for the op.
        kernel_name (str): name of op.
        attrs (dict): tiling parameter.
        log_cce (bool): False by default.
        dump_ir (bool): True by default.
        dump_code (bool): False by default.
        polyhedral (bool): True by default.
        tuning (bool): False by default.

    Return:
        module.
    """
    inputs = []
    set_dim_key = ""
    shape_params = []
    for i, (shape, dtype) in enumerate(zip(input_shapes, input_types)):
        if isinstance(shape, (list, tuple)) and shape and isinstance(shape[0], (list, tuple)):
            tmp_input = []
            for j, tmp_shape in enumerate(shape):
                tmp_input.append(akg.tvm.placeholder(tmp_shape, dtype, "input_%d_%d" % (i + 1, j + 1)))
                for tmp in tmp_shape:
                    if isinstance(tmp, akg.tvm.expr.Var):
                        shape_params.append(tmp)
            inputs.append(tmp_input)
        elif isinstance(shape, (list, tuple)) and shape and isinstance(shape[0], akg.tvm.expr.Var):
            inputs.append(akg.tvm.placeholder(shape, dtype, "input_%d" % (i + 1)))
            for tmp_shape in shape:
                if isinstance(tmp_shape, akg.tvm.expr.Var):
                    shape_params.append(tmp_shape)
        elif isinstance(shape, akg.tvm.tensor.Tensor):
            inputs.append(shape)
            for tmp_shape in shape.shape:
                shape_params.append(tmp_shape)
        else:
            inputs.append(akg.tvm.placeholder(shape, dtype, "input_%d" % (i + 1)))
    attrs_params = []
    if op_attrs is not None:
        args = inputs + op_attrs
        for tmp_attr in op_attrs:
            if isinstance(tmp_attr, (list, tuple)) and tmp_attr and isinstance(tmp_attr[0], akg.tvm.expr.Var):
                for attr_param in tmp_attr:
                    if isinstance(attr_param, akg.tvm.expr.Var):
                        attrs_params.append(attr_param)
            elif isinstance(tmp_attr, akg.tvm.expr.Var):
                attrs_params.append(tmp_attr)
    else:
        args = inputs

    # backup inputs because the tensor names may be updated inside op_func
    inputs_backup = recursive_copy(inputs)

    output = op_func(*args)

    # restore inputs to make sure that tensor names are not changed by op_func
    inputs = inputs_backup

    if attrs is None or 'dim' not in attrs or not attrs['dim']:
        dim_info = ""
        if attrs is None:
            attrs = dict()

        if op_func.__name__ in ct_util.set_dim_func_map.keys():
            value = ct_util.set_dim_func_map[op_func.__name__]
            if inspect.isfunction(value):
                dim_info = value(*args)
            elif isinstance(value, dict):
                key = []
                key.append(ft_util.convert_to_list(input_shapes))
                key.append(ft_util.convert_to_list(input_types))
                if op_attrs is not None:
                    key.append(op_attrs)
                key = str(tuple(key))

                if key in value.keys():
                    dim_info = ct_util.set_dims(value[key])
            else:
                raise RuntimeError("Registered set_dim_map is invalid. Must be a function or a dict!")
        if isinstance(dim_info, (list, tuple)):
            dim_info = dim_info[0]

        attrs['dim'] = dim_info

    compute_func = None  # func which is defined in dsl for doing compute_inline or other
    sch_tmpl = None
    if isinstance(output, (list, tuple)):
        from inspect import isfunction
        new_outputs = []
        for elem in output:
            if isfunction(elem):
                compute_func = elem
            elif isinstance(elem, dict):
                for key, value in elem.items():
                    if key not in attrs or not attrs[key]:
                        attrs[key] = value
            elif isinstance(elem, (list, tuple)):
                new_outputs += elem
            else:
                new_outputs.append(elem)

        output = new_outputs
    elif isinstance(output, dict):
        sch_tmpl = output
        output = sch_tmpl['output']
    binds = None if not attrs else attrs.pop(BINDS, None)

    op_var = []
    for xx in inputs:
        if isinstance(xx, list):
            for x in xx:
                op_var.append(x)
        else:
            op_var.append(xx)
    shape_var = []
    if attrs_params:
        [shape_var.append(i) for i in attrs_params if i not in shape_var]
    [shape_var.append(i) for i in shape_params if i not in shape_var]
    if isinstance(output, (list, tuple)):
        op_var = op_var + [i for i in output if TensorUtils.is_output_value(i)]
    else:
        if TensorUtils.is_output_value(output):
            op_var = op_var + [output]

    if sch_tmpl is not None:
        if sch_tmpl['target'] != 'cuda':
            raise ValueError("Only support cuda as target when using schedule template.")
        kernel_name = kernel_name if kernel_name != "" else sch_tmpl['op_name']
        with akg.tvm.target.cuda() as target:
            s = sch_tmpl['schedule'](sch_tmpl['output'])
            with akg.tvm.build_config(dump_pass_ir=dump_ir):
                mod = akg.build(s, op_var, "cuda", shape_var, name=kernel_name, attrs=attrs,
                                polyhedral=polyhedral, binds=binds)
                dump_cuda_meta.dump(mod, kernel_name, s, op_var)
                if dump_code:
                    source_code = mod.imported_modules[0].get_source()
                    create_code(kernel_name, "./", source_code, "CUDA")
                return mod

    if isinstance(output, (list, tuple)):
        tmp = []
        for x in list(output):
            if isinstance(x, tuple):
                tmp.append(x[0].op)
            else:
                tmp.append(x.op)
        s = akg.tvm.create_schedule(tmp)
    else:
        s = akg.tvm.create_schedule(output.op)
    if compute_func is not None:
        compute_func(s)
        polyhedral = False
    kernel_name = kernel_name if kernel_name != "" else op_func.__name__
    mode = get_runtime_mode()
    level = attrs.get("help_tiling")
    if tuning or (level is not None and level > help_tiling_level['None']):
        if op_func.__name__ in ct_util.set_dim_func_map.keys():
            func_ = ct_util.set_dim_func_map[op_func.__name__]
            if inspect.isfunction(func_):
                set_dim_key = func_(*args)[1]
        elif op_func.__name__ in ct_util.gen_key_func_map.keys():
            func_ = ct_util.gen_key_func_map[op_func.__name__]
            if inspect.isfunction(func_):
                set_dim_key = func_(*args)
        with akg.build_config(add_lower_pass=cce.debug_mode(0), dump_pass_ir=True):
            spaces = akg.lower(s, op_var, name=kernel_name, attrs=attrs, polyhedral=polyhedral, tuning=tuning)
            if set_dim_key == "":
                set_dim_key = str(args)
            return spaces, set_dim_key

    if mode == "cpu":
        mod = akg.tvm.build(s, op_var, "llvm")
        if not os.path.isdir("./cpu/ir/"):
            os.makedirs("./cpu/ir/")
        with os.fdopen(os.open("./cpu/ir/" + kernel_name + ".cc", os.O_WRONLY | os.O_CREAT, 0o400), 'w') as irf:
            irf.write(akg.tvm.lower(s, op_var, shape_var, simple_mode=True))
        return mod
    with akg.build_config(add_lower_pass=cce.debug_mode(0), dump_pass_ir=dump_ir):
        mod = akg.build(s, op_var, "cce", shape_var, name=kernel_name, attrs=attrs, polyhedral=polyhedral, binds=binds)
        if mod is None:
            return None
        source_code = mod.imported_modules[0].get_source()
    if log_cce:
        logging.debug("#################cce code####################")
        logging.debug(source_code)
    if dump_code:
        code_path = "./"
        create_code(kernel_name, code_path, source_code)

    return mod


def get_runtime_mode():
    """get runtime mode."""
    env_dic = os.environ
    if not env_dic.get('RUNTIME_MODE'):
        mode = 'rpc_cloud'
    else:
        mode = env_dic.get('RUNTIME_MODE')
    return mode


def get_profiling_mode():
    """get profiling mode."""
    env_dic = os.environ
    if env_dic.get('PROFILING_MODE') and env_dic.get('PROFILING_MODE').lower() == "true":
        return True
    return False


def product_is_mini():
    """check whether in mini environment."""
    mode = get_runtime_mode()
    if mode in ('rpc', 'air', 'aic', 'compile_mini'):
        return True
    return False


def get_available_devices_num():
    """get available devives num."""
    env_dic = os.environ
    try:
        return int(env_dic.get('DEVICE_TOTAL_NUM').lower()) if env_dic.get('DEVICE_TOTAL_NUM') else 1
    except NameError as e:
        logging.error(e)
        return 1


def get_device_id():
    """get device id."""
    env_dic = os.environ
    try:
        return int(env_dic.get('DEVICE_ID').lower()) if env_dic.get('DEVICE_ID') else 0
    except NameError as e:
        logging.error(e)
        return 0


class TestUtils:
    """Class for getting cycle and core num."""
    @staticmethod
    def record_cycle(cycle):
        if os.environ.get(PERFORMANCE_TEST_FILE):
            result_file = os.environ.get(PERFORMANCE_TEST_FILE)
            with open(result_file, "a+") as f:
                f.write("{0}\n".format(cycle))

    @staticmethod
    def record_core(stmt):
        """Function for getting performance data from cores."""
        def get_core_num():
            core_num = 1
            if hasattr(stmt, 'attr_key') and stmt.attr_key == 'thread_extent':
                core_num = stmt.value
            return core_num
        if os.environ.get(PERFORMANCE_TEST_FILE):
            result_file = os.environ.get(PERFORMANCE_TEST_FILE)
            with open(result_file, "a+") as f:
                f.write("{0}; ".format(get_core_num()))
