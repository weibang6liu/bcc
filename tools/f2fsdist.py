#!/usr/bin/python
# SPDX-License-Identifier: <SPDX License Expression>
# @lint-avoid-python-3-compatibility-imports
#
# f2fsdist  Summarize f2fs operation latency.
#           For Linux, uses BCC, eBPF.
#
# USAGE: f2fsdist [-h] [-T] [-m] [-p PID] [interval] [count]
#
# Copyright (c) 2022, Samsung Electronics.  All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License")
# thanks for Brendan Gregg's ext4dist
# (https://github.com/iovisor/bcc/blob/master/tools/ext4dist.py) reference.
#
# 28-Jul-2022 Ting Zhang<ting03.zhang@samsung.com>    Created this.

from __future__ import print_function
from bcc import BPF
from time import sleep, strftime
import argparse

# symbols
kallsyms = "/proc/kallsyms"

# arguments
examples = """examples:
    ./f2fsdist            # show operation latency as a histogram
    ./f2fsdist -p 181     # trace PID 181 only
    ./f2fsdist 1 10       # print 1 second summaries, 10 times
    ./f2fsdist -m 5       # 5s summaries, milliseconds
"""
# paras description
parser = argparse.ArgumentParser(
    description="Summarize f2fs operation latency",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=examples)
parser.add_argument("-T", "--notimestamp", action="store_true",
                    help="don't include timestamp on interval output")
parser.add_argument("-m", "--milliseconds", action="store_true",
                    help="output in milliseconds")
parser.add_argument("-p", "--pid",
                    help="trace this PID only")
parser.add_argument("interval", nargs="?",
                    help="output interval, in seconds")
parser.add_argument("count", nargs="?", default=99999999,
                    help="number of outputs")
parser.add_argument("--ebpf", action="store_true",
                    help=argparse.SUPPRESS)
args = parser.parse_args()
pid = args.pid
countdown = int(args.count)

if args.milliseconds:
    factor = 1000000
    label = "msecs"
else:
    factor = 1000
    label = "usecs"
if args.interval and int(args.interval) == 0:
    print("ERROR: interval 0. Exiting.")
    exit()
debug = 0

# define BPF program
bpf_text = """
#include <uapi/linux/ptrace.h>
#include <linux/fs.h>
#include <linux/sched.h>
#define OP_NAME_LEN 8
typedef struct dist_key{
    char op[OP_NAME_LEN];
    u64 slot;
} dist_key_t;
BPF_HASH(start, u32);
BPF_HISTOGRAM(dist, dist_key_t);

// time operation
int trace_entry(struct pt_regs *ctx)
{
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u32 pid = pid_tgid >> 32;
    u32 tid = (u32)pid_tgid;

    if (FILTER_PID)
        return 0;
    u64 ts = bpf_ktime_get_ns();
    start.update(&tid, &ts);
    return 0;
}
F2FS_TRACE_READ_CODE
static int trace_return(struct pt_regs *ctx, const char *op)
{
    u64 *tsp;
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u32 pid = pid_tgid >> 32;
    u32 tid = (u32)pid_tgid;

    // fetch timestamp and calculate delta
    tsp = start.lookup(&tid);
    if (tsp == 0) {
        return 0;   // missed start or filtered
    }
    u64 delta = bpf_ktime_get_ns() - *tsp;
    start.delete(&tid);

    if((s64) delta <0)
        return 0;

    delta /= FACTOR;

    // store as histogram
    dist_key_t key = {.slot = bpf_log2l(delta)};
    __builtin_memcpy(&key.op, op, sizeof(key.op));
    dist.increment(key);

    return 0;
}

int trace_read_return(struct pt_regs *ctx)
{
    char *op = "read";
    return trace_return(ctx, op);
}
int trace_write_return(struct pt_regs *ctx)
{
    char *op = "write";
    return trace_return(ctx, op);
}
int trace_open_return(struct pt_regs *ctx)
{
    char *op = "open";
    return trace_return(ctx, op);
}
int trace_fsync_return(struct pt_regs *ctx)
{
    char *op = "fsync";
    return trace_return(ctx, op);
}
"""
# Starting from Linux 4.10 f2fs_file_operations.read_iter has been changed from
# using generic_file_read_iter() to its own f2fs_file_read_iter().
#
# To detect the proper function to trace check if f2fs_file_read_iter() is
# defined in /proc/kallsyms, if it's defined attach to that function, otherwise
# use generic_file_read_iter() and inside the trace hook filter on f2fs read
# events (checking if file->f_op == f2fs_file_operations).

if BPF.get_kprobe_functions(b'f2fs_file_read_iter'):
    f2fs_read_fn = 'f2fs_file_read_iter'
    f2fs_trace_read_fn = 'trace_entry'
    f2fs_trace_read_code = ''
else:
    f2fs_read_fn = 'generic_file_read_iter'
    f2fs_trace_read_fn = 'trace_read_entry'
    f2fs_file_ops_addr = ''
    with open(kallsyms) as syms:
        for line in syms:
            (addr, size, name) = line.rstrip().split(" ", 2)
            name = name.split("\t")[0]
            if name == "f2fs_file_operations":
                f2fs_file_ops_addr = "0x" + addr
                break
        if f2fs_file_ops_addr == '':
            print("ERROR: no f2fs_file_operations in /proc/kallsyms. Exiting.")
            print("HINT: the kernel should be built with CONFIG_KALLSYMS_ALL.")
            exit()

    f2fs_trace_read_code = """
int trace_read_entry(struct pt_regs *ctx, struct kiocb *iocb)
{
    u64 pid_tgid = bpf_get_current_pid_tgid();
    u32 pid = pid_tgid >> 32;
    u32 tid = (u32)pid_tgid;

    if (FILTER_PID)
        return 0;

    // f2fs filter on file->f_op == f2fs_file_operations
    struct file *fp = iocb->ki_filp;
    if ((u64)fp->f_op != %s)
        return 0;

    u64 ts = bpf_ktime_get_ns();
    start.update(&tid, &ts);
    return 0;
}""" % f2fs_file_ops_addr

# code replacements
bpf_text = bpf_text.replace('F2FS_TRACE_READ_CODE', f2fs_trace_read_code)
bpf_text = bpf_text.replace('FACTOR', str(factor))
if args.pid:
    bpf_text = bpf_text.replace('FILTER_PID', 'pid != %s' % pid)
else:
    bpf_text = bpf_text.replace('FILTER_PID', '0')
if debug or args.ebpf:
    print(bpf_text)
    if args.ebpf:
        exit()

# load BPF program
b = BPF(text=bpf_text)

b.attach_kprobe(event=f2fs_read_fn, fn_name=f2fs_trace_read_fn)
b.attach_kprobe(event="f2fs_file_write_iter", fn_name="trace_entry")
b.attach_kprobe(event="f2fs_file_open", fn_name="trace_entry")
b.attach_kprobe(event="f2fs_sync_file", fn_name="trace_entry")
b.attach_kretprobe(event=f2fs_read_fn, fn_name='trace_read_return')
b.attach_kretprobe(event="f2fs_file_write_iter", fn_name='trace_write_return')
b.attach_kretprobe(event="f2fs_file_open", fn_name='trace_open_return')
b.attach_kretprobe(event="f2fs_sync_file", fn_name='trace_fsync_return')
print("Tracing f2fs operation latency... Hit Ctrl-C to end.")

# output
exiting = 0
dist = b.get_table("dist")
while (1):
    try:
        if args.interval:
            sleep(int(args.interval))
        else:
            sleep(99999999)
    except KeyboardInterrupt:
        exiting = 1

    print()
    if args.interval and (not args.notimestamp):
        print(strftime("%H:%M:%S:"))

    dist.print_log2_hist(label, "operation", section_print_fn=bytes.decode)
    dist.clear()

    countdown -= 1
    if exiting or countdown == 0:
        exit()
