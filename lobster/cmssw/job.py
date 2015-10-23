from collections import defaultdict
import gzip
import imp
import json
import logging
import os
import re
import shutil
import subprocess
import sys

from lobster import fs, job, util
from lobster.cmssw import TaskHandler
import dash
import sandbox

import jobit
from dataset import MetaInterface

import work_queue as wq

logger = logging.getLogger('lobster.cmssw.job')

class ReleaseSummary(object):
    """Summary of returned tasks.

    Prints a user-friendly summary of which tasks returned with what exit code/status.
    """

    flags = {
            wq.WORK_QUEUE_RESULT_INPUT_MISSING: "missing input",                # 1
            wq.WORK_QUEUE_RESULT_OUTPUT_MISSING: "missing output",              # 2
            wq.WORK_QUEUE_RESULT_STDOUT_MISSING: "no stdout",                   # 4
            wq.WORK_QUEUE_RESULT_SIGNAL: "signal received",                     # 8
            wq.WORK_QUEUE_RESULT_RESOURCE_EXHAUSTION: "exhausted resources",    # 16
            wq.WORK_QUEUE_RESULT_TASK_TIMEOUT: "time out",                      # 32
            wq.WORK_QUEUE_RESULT_UNKNOWN: "unclassified error",                 # 64
            wq.WORK_QUEUE_RESULT_FORSAKEN: "unrelated error",                   # 128
            wq.WORK_QUEUE_RESULT_MAX_RETRIES: "exceed # retries",               # 256
            wq.WORK_QUEUE_RESULT_TASK_MAX_RUN_TIME: "exceeded runtime"          # 512
    }

    def __init__(self):
        self.__exe = {}
        self.__wq = {}
        self.__taskdirs = {}

    def exe(self, status, taskid):
        try:
            self.__exe[status].append(taskid)
        except KeyError:
            self.__exe[status] = [taskid]

    def wq(self, status, taskid):
        for flag in ReleaseSummary.flags.keys():
            if status & flag:
                try:
                    self.__wq[flag].append(taskid)
                except KeyError:
                    self.__wq[flag] = [taskid]

    def dir(self, taskid, taskdir):
        self.__taskdirs[taskid] = taskdir

    def __str__(self):
        s = "received the following task(s):\n"
        for status in sorted(self.__exe.keys()):
            s += "returned with status {0}: {1}\n".format(status, ", ".join(self.__exe[status]))
            if status != 0:
                s += "parameters and logs in:\n\t{0}\n".format(
                        "\n\t".join([self.__taskdirs[t] for t in self.__exe[status]]))
        for flag in sorted(self.__wq.keys()):
            s += "failed due to {0}: {1}\nparameters and logs in:\n\t{2}\n".format(
                    ReleaseSummary.flags[flag],
                    ", ".join(self.__wq[flag]),
                    "\n\t".join([self.__taskdirs[t] for t in self.__wq[flag]]))
        # Trim final newline
        return s[:-1]

class JobProvider(job.JobProvider):
    def __init__(self, config, interval=300):
        super(JobProvider, self).__init__(config)

        self.bad_exitcodes += [169]
        self.__interval = interval  # seconds
        self.__dash = None
        self.__dash_checker = dash.JobStateChecker(interval)

        if 'merge size' in self.config:
            bytes = self.config['merge size']
            orig = bytes
            if isinstance(bytes, basestring):
                unit = bytes[-1].lower()
                try:
                    bytes = float(bytes[:-1])
                    if unit == 'k':
                        bytes *= 1000
                    elif unit == 'm':
                        bytes *= 1e6
                    elif unit == 'g':
                        bytes *= 1e9
                    else:
                        bytes = -1
                except ValueError:
                    bytes = -1
                self.config['merge size'] = bytes

            if bytes > 0:
                logger.info('merging outputs up to {0} bytes'.format(bytes))
            else:
                logger.error('merging disabled due to malformed size {0}'.format(orig))

        self.__sandbox = os.path.join(self.workdir, 'sandbox')
        self.__jobhandlers = {}
        self.__interface = MetaInterface()
        self.__store = jobit.JobitStore(self.config)

        self._inputs = [(self.__sandbox + ".tar.bz2", "sandbox.tar.bz2", True),
                (os.path.join(os.path.dirname(__file__), 'data', 'siteconfig'), 'siteconfig', True),
                (os.path.join(os.path.dirname(__file__), 'data', 'wrapper.sh'), 'wrapper.sh', True),
                (os.path.join(os.path.dirname(__file__), 'data', 'job.py'), 'job.py', True),
                (self.parrot_bin, 'bin', None),
                (self.parrot_lib, 'lib', None),
                ]

        # Files to make the job wrapper work without referencing WMCore
        # from somewhere else
        import WMCore
        base = os.path.dirname(WMCore.__file__)
        reqs = [
                "__init__.py",
                "__init__.pyc",
                "Algorithms",
                "Configuration.py",
                "Configuration.pyc",
                "DataStructs",
                "FwkJobReport",
                "Services/__init__.py",
                "Services/__init__.pyc",
                "Services/Dashboard",
                "WMException.py",
                "WMException.pyc",
                "WMExceptions.py",
                "WMExceptions.pyc"
                ]
        for f in reqs:
            self._inputs.append((os.path.join(base, f), os.path.join("python", "WMCore", f), True))

        if 'X509_USER_PROXY' in os.environ:
            self._inputs.append((os.environ['X509_USER_PROXY'], 'proxy', False))

        if not util.checkpoint(self.workdir, 'executable'):
            # We can actually have more than one exe name (one per task label)
            # Set 'cmsRun' if any of the tasks are of that type,
            # or use cmd command if all tasks execute the same cmd,
            # or use 'noncmsRun' if task cmds are different
            # Using this for dashboard exe name reporting
            cmsconfigs = [cfg.get('cmssw config') for cfg in self.config['tasks']]
            cmds = [cfg.get('cmd') for cfg in self.config['tasks']]
            if any(cmsconfigs):
                exename = 'cmsRun'
            elif all(x == cmds[0] and x is not None for x in cmds):
                exename = cmds[0]
            else:
                exename = 'noncmsRun'

            util.register_checkpoint(self.workdir, 'executable', exename)

        if self.config.get('use dashboard', True):
            logger.info("using dashboard with task id {0}".format(self.taskid))
            monitor = dash.Monitor
        else:
            monitor = dash.DummyMonitor

        if not util.checkpoint(self.workdir, 'sandbox'):
            blacklist = self.config.get('sandbox blacklist', [])
            cmssw_version = sandbox.package(self.config.get('sandbox release top', os.environ['LOCALRT']),
                                            self.__sandbox, blacklist, self.config.get('recycle sandbox'))
            util.register_checkpoint(self.workdir, 'sandbox', 'CREATED')
            util.register_checkpoint(self.workdir, 'sandbox cmssw version', cmssw_version)
            self.__dash = monitor(self.workdir)
            self.__dash.register_run()

        else:
            self.__dash = monitor(self.workdir)
            for id in self.__store.reset_jobits():
                self.__dash.update_job(id, dash.ABORTED)

        update_config = False

        for label, wflow in self.workflows.items():
            # FIXME this needs to be in the Workflow class!
            if wflow.pset:
                 shutil.copy(util.findpath(self.basedirs, wflow.pset), os.path.join(wflow.workdir, os.path.basename(wflow.pset)))

            if wflow.pset and not wflow._outputs:
                wflow._outputs = []
                # Save determined outputs to the configuration in the
                # working directory.
                update_config = True

                # To avoid problems loading configs that use the VarParsing module
                sys.argv = ["pacify_varparsing.py"]
                with open(util.findpath(self.basedirs, wflow.pset), 'r') as f:
                    source = imp.load_source('cms_config_source', wflow.pset, f)
                    process = source.process
                    if hasattr(process, 'GlobalTag') and hasattr(process.GlobalTag.globaltag, 'value'):
                        cfg['global tag'] = process.GlobalTag.globaltag.value()
                    for label, module in process.outputModules.items():
                        wflow._outputs.append(module.fileName.value())
                    if 'TFileService' in process.services:
                        wflow._outputs.append(process.services['TFileService'].fileName.value())
                        wflow.edm_output = False

                    wflow.config['edm output'] = wflow.edm_output
                    wflow.config['outputs'] = wflow._outputs

                    logger.info("workflow {0}: adding output file(s) '{1}'".format(label, ', '.join(wflow._outputs)))

            if not util.checkpoint(self.workdir, label):
                if wflow.pset:
                    shutil.copy(util.findpath(self.basedirs, wflow.pset), os.path.join(wflow.workdir, os.path.basename(wflow.pset)))

                logger.info("querying backend for {0}".format(label))
                with fs.default():
                    dataset_info = self.__interface.get_info(wflow.config)

                logger.info("registering {0} in database".format(label))
                self.__store.register(wflow.config, dataset_info, wflow.runtime)
                util.register_checkpoint(self.workdir, label, 'REGISTERED')
            elif os.path.exists(os.path.join(wflow.workdir, 'running')):
                for id in self.get_jobids(label):
                    util.move(wflow.workdir, id, 'failed')

        if update_config:
            self.save_configuration()

    def get_report(self, label, job):
        return os.path.join(self.workdir, label, 'successful', util.id2dir(job), 'report.json')

    def obtain(self, num=1):
        jobinfos = self.__store.pop_unmerged_jobs(self.config.get('merge size', -1), 10) \
                + self.__store.pop_jobits(num)
        if not jobinfos or len(jobinfos) == 0:
            return None

        tasks = []
        ids = []

        for (id, label, files, lumis, unique_arg, empty_source, merge) in jobinfos:
            wflow = self.workflows[label]
            ids.append(id)

            jdir = util.taskdir(wflow.workdir, id)
            inputs = list(self._inputs)
            inputs.append((os.path.join(jdir, 'parameters.json'), 'parameters.json', False))
            outputs = [(os.path.join(jdir, f), f) for f in ['executable.log.gz', 'report.json']]

            monitorid, syncid = self.__dash.register_job(id)

            config = {
                'mask': {
                    'files': None,
                    'lumis': None,
                    'events': None
                },
                'monitoring': {
                    'monitorid': monitorid,
                    'syncid': syncid,
                    'taskid': self.taskid
                },
                'arguments': None,
                'output files': None,
                'want summary': self.config.get('cmssw summary', True),
                'executable': None,
                'pset': None,
                'prologue': self.config.get('prologue'),
                'epilogue': None
            }

            if merge:
                missing = []
                infiles = []
                inreports = []

                for job, _, _, _ in lumis:
                    report = self.get_report(label, job)
                    _, infile = list(wflow.outputs(job))[0]

                    if os.path.isfile(report):
                        inreports.append(report)
                        infiles.append((job, infile))
                    else:
                        missing.append(job)

                if len(missing) > 0:
                    template = "the following have been marked as failed because their output could not be found: {0}"
                    logger.warning(template.format(", ".join(map(str, missing))))
                    self.__store.update_missing(missing)

                if len(infiles) <= 1:
                    # FIXME report these back to the database and then skip
                    # them.  Without failing these job ids, accounting of
                    # running jobs is going to be messed up.
                    logger.debug("skipping job {0} with only one input file!".format(id))

                # takes care of the fields set to None in config
                wflow.adjust(config, jdir, inputs, outputs, merge, reports=inreports)

                files = infiles
            else:
                # takes care of the fields set to None in config
                wflow.adjust(config, jdir, inputs, outputs, merge, unique=unique_arg)

            handler = TaskHandler(
                id, label, files, lumis, list(wflow.outputs(id)),
                jdir, wflow.pset is not None, empty_source,
                merge=merge,
                local=wflow.local)

            # set input/output transfer parameters
            self._storage.preprocess(config, merge)
            # adjust file and lumi information in config, add task specific
            # input/output files
            handler.adjust(config, inputs, outputs, self._storage)

            with open(os.path.join(jdir, 'parameters.json'), 'w') as f:
                json.dump(config, f, indent=2)

            cmd = 'sh wrapper.sh python job.py parameters.json'

            cores = 1 if merge else self.config.get('cores per job', 1)
            runtime = None
            if 'task runtime' in config:
                runtime = config['task runtime'] + 15 * 60

            tasks.append((runtime, cores, cmd, id, inputs, outputs))

            self.__jobhandlers[id] = handler

        logger.info("creating job(s) {0}".format(", ".join(map(str, ids))))

        self.__dash.free()

        return tasks

    def release(self, tasks):
        cleanup = []
        jobs = defaultdict(list)
        summary = ReleaseSummary()
        for task in tasks:
            failed = (task.return_status != 0)

            handler = self.__jobhandlers[task.tag]

            self.__dash.update_job(task.tag, dash.DONE)

            if task.output:
                f = gzip.open(os.path.join(handler.jobdir, 'job.log.gz'), 'wb')
                f.write(task.output)
                f.close()

            job_update = jobit.JobUpdate()
            files_info = {}
            files_skipped = []
            cmssw_exit_code = None
            events_written = 0
            try:
                with open(os.path.join(handler.jobdir, 'report.json'), 'r') as f:
                    data = json.load(f)
                    job_update.cache = data['cache']['type']
                    job_update.cache_end_size = data['cache']['end size']
                    job_update.cache_start_size = data['cache']['start size']
                    job_update.time_wrapper_start = data['task timing']['time wrapper start']
                    job_update.time_wrapper_ready = data['task timing']['time wrapper ready']
                    job_update.time_stage_in_end = data['task timing']['time stage in end']
                    job_update.time_prologue_end = data['task timing']['time prologue end']
                    job_update.time_file_requested = data['task timing']['time file requested']
                    job_update.time_file_opened = data['task timing']['time file opened']
                    job_update.time_file_processing = data['task timing']['time file processing']
                    job_update.time_processing_end = data['task timing']['time processing end']
                    job_update.time_epilogue_end = data['task timing']['time epilogue end']
                    job_update.time_stage_out_end = data['task timing']['time stage out end']
                    job_update.time_cpu = data['cpu time']
                    if handler.cmssw_job:
                        files_info = data['files']['info']
                        files_skipped = data['files']['skipped']
                        events_written = data['events written']
                        cmssw_exit_code = data['cmssw exit code']
                        job_update.bytes_output = data['output size']
                        job_update.bytes_bare_output = data['output bare size']
            except (ValueError, EOFError) as e:
                failed = True
                logger.error("error processing {0}:\n{1}".format(task.tag, e))
            except IOError as e:
                failed = True
                logger.error("error processing {1} from {0}".format(task.tag, os.path.basename(e.filename)))

            if not task.return_status and task.result != wq.WORK_QUEUE_RESULT_SUCCESS:
                exit_code = 100000 + task.result
                failed = True
                summary.wq(task.result, task.tag)
            elif cmssw_exit_code not in (None, 0):
                exit_code = cmssw_exit_code
                if exit_code > 0:
                    failed = True
                summary.exe(exit_code, task.tag)
            else:
                exit_code = task.return_status
                summary.exe(exit_code, task.tag)

            jobits_processed, events_read, events_written, status, file_update, jobit_update = \
                    handler.get_jobit_info(failed, files_info, files_skipped, events_written)

            job_update.bytes_received = task.total_bytes_received
            job_update.bytes_sent = task.total_bytes_sent
            job_update.events_read = events_read
            job_update.events_written = events_written
            job_update.exit_code = exit_code
            job_update.host = util.verify_string(task.hostname)
            job_update.id = task.tag
            job_update.jobits_processed = jobits_processed
            job_update.limits_exceeded = task.resources_measured.limits_exceeded
            job_update.memory_resident = task.resources_measured.resident_memory
            job_update.memory_swap = task.resources_measured.swap_memory
            job_update.memory_virtual = task.resources_measured.virtual_memory
            job_update.status = status
            job_update.submissions = task.total_submissions
            job_update.time_submit = task.submit_time / 1000000
            job_update.time_transfer_in_start = task.send_input_start / 1000000
            job_update.time_transfer_in_end = task.send_input_finish / 1000000
            job_update.time_transfer_out_start = task.receive_output_start / 1000000
            job_update.time_transfer_out_end = task.receive_output_finish / 1000000
            job_update.time_retrieved = task.finish_time / 1000000
            job_update.time_on_worker = task.cmd_execution_time / 1000000
            job_update.time_total_on_worker = task.total_cmd_execution_time / 1000000
            job_update.workdir_footprint = task.resources_measured.workdir_footprint
            job_update.workdir_num_files = task.resources_measured.workdir_num_files

            wflow = self.workflows[handler.dataset]
            if failed:
                faildir = util.move(wflow.workdir, handler.id, 'failed')
                summary.dir(str(handler.id), faildir)
                cleanup += [lf for rf, lf in handler.outputs]
            else:
                if handler.merge and self.config.get('delete merged', True):
                    files = handler.input_files
                    cleanup += files
                util.move(wflow.workdir, handler.id, 'successful')

            self.__dash.update_job(task.tag, dash.RETRIEVED)

            jobs[(handler.dataset, handler.jobit_source)].append((job_update, file_update, jobit_update))

            del self.__jobhandlers[task.tag]

        self.__dash.free()

        if len(cleanup) > 0:
            try:
                fs.remove(*cleanup)
            except (IOError, OSError):
                pass
            except ValueError as e:
                logger.error("error removing {0}:\n{1}".format(task.tag, e))

        if len(jobs) > 0:
            logger.info(summary)
            self.__store.update_jobits(jobs)

    def terminate(self):
        for id in self.__store.running_jobs():
            self.__dash.update_job(str(id), dash.CANCELLED)

    def done(self):
        left = self.__store.unfinished_jobits()
        if self.config.get('merge size', -1) > 0:
            return self.__store.merged() and left == 0
        return left == 0

    def __update_dashboard(self, queue, exclude_states):
        try:
            self.__dash_checker.update_dashboard_states(self.__dash, queue, exclude_states)
        except:
            logger.warning("Could not update job states to dashboard")

    def update(self, queue):
        # update dashboard status for all unfinished tasks.
        # WAITING_RETRIEVAL is not a valid status in dashboard,
        # so skipping it for now.
        exclude_states = ( dash.DONE, dash.WAITING_RETRIEVAL )
        self.__update_dashboard(queue, exclude_states)

    def tasks_left(self):
        return self.__store.estimate_tasks_left()

    def work_left(self):
        return self.__store.unfinished_jobits()
