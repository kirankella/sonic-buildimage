#!/usr/bin/env python

import os
import time
import tarfile
import socket
import yaml
import json
import syslog
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from azure.storage.file import FileService

global CORE_FILE_PATH, RC_FILE
global hostname, sonicversion, asicname, acctname, acctkey, sharename, cwd
global INIT_CWD
global log_level
global this_file

this_file = os.path.basename(__file__)

global cfg
cfg = ""

CORE_FILE_PATH = "/var/core/"
RC_FILE = "/etc/sonic/core_analyzer.rc.json"
INIT_CWD = "/tmp"

hostname = ""
sonicversion = ""
asicname = ""
acctname = ""
acctkey = ""
sharename = ""
cwd = []

HOURS_4 = (4 * 60 * 60)
PAUSE_ON_FAIL = (60 * 60)
MAX_RETRIES = 5

log_level = syslog.LOG_DEBUG

def log_msg(lvl, fname, m):
    if (lvl <= log_level):
        syslog.syslog(lvl, "{}: {}".format(fname, m))

    if log_level == syslog.LOG_DEBUG:
        print("{}: {}".format(fname, m))

def log_err(m):
    log_msg(syslog.LOG_ERR, this_file, m)

def log_info(m):
    log_msg(syslog.LOG_INFO, this_file, m)

def log_warn(m):
    log_msg(syslog.LOG_WARNING, this_file, m)

def log_debug(m):
    log_msg(syslog.LOG_DEBUG, this_file, m)


def make_new_dir(p):
    os.system("rm -rf " + p)
    os.system("mkdir -p " + p)

def parse_a_json(data, prefix, val):
    for i in data:
        if type(data[i]) == dict:
            parse_a_json(data[i], prefix + (i,), val)
        else:
            val[prefix + (i,)] = data[i]

class config:
    parsed_data = {}
    cfg_data = {}

    def __init__(self):
        while not os.path.exists(RC_FILE):
            # Wait here until service restart
            log_err("Unable to retrieve Azure storage credentials")
            time.sleep (HOURS_4)

        with open(RC_FILE, 'r') as f:
            self.parsed_data = json.load(f)
            parse_a_json(self.parsed_data, (), self.cfg_data)

    def get_data(self, k):
        return self.cfg_data[k] if self.cfg_data.has_key(k) else ""

    def get_dict(self):
        return self.parsed_data

    def get_core_info(self, corepath, devicename):
        info = {}
        info["corefname"] = os.path.basename(corepath)
        info["tstamp"] = str(os.stat(corepath).st_ctime)
        info["devicename"] = devicename

        lpath = self.get_data(("metadata_files_in_archive", "core_info"))
        f = open(lpath, "w+")
        f.write(json.dumps(info, indent=4))
        f.close()

        return lpath


class Watcher:

    def __init__(self):
        self.observer = Observer()

    def run(self):
        event_handler = Handler()
        self.observer.schedule(event_handler, CORE_FILE_PATH)
        self.observer.start()
        try:
            while True:
                time.sleep(5)
        except:
            self.observer.stop()
            log_err("Error in watcher")

        self.observer.join()

def set_env(lst):
    for k in lst:
        if lst[k]:
            os.environ[k] = lst[k]
            log_debug("set env {} = {}".format(k, lst[k]))

class Handler(FileSystemEventHandler):

    @staticmethod
    def init():
        global hostname, sonicversion, asicname, acctname, acctkey, sharename
        global cwd, cfg

        cfg = config()

        set_env(cfg.get_dict()["env"])

        hostname = socket.gethostname()
        if not hostname:
            raise Exception("Failed to read hostname")

        acctname = cfg.get_data(("azure_sonic_core_storage", "account_name"))
        acctkey = cfg.get_data(("azure_sonic_core_storage", "account_key"))
        sharename = cfg.get_data(("azure_sonic_core_storage", "share_name"))

        if not acctname or not acctkey or not sharename:
            while True:
                # Wait here until service restart
                log_err("Unable to retrieve Azure storage credentials")
                time.sleep (HOURS_4)

        with open("/etc/sonic/sonic_version.yml", 'r') as stream:
            l = yaml.safe_load(stream)
            sonicversion = l['build_version']
            asicname = l['asic_type']

        if not sonicversion:
            raise Exception("Failed to read build_version from /etc/sonic/sonic_version.yml")

        if not asicname:
            raise Exception("Failed to read asic_type from /etc/sonic/sonic_version.yml")

        cwd = cfg.get_data(("local_work", "core_upload")).split("/")
        if not len(cwd) > 2:
            raise Exception("Invalid path for core_upload. Expect a min of two elements in path")

        os.chdir(INIT_CWD)

    @staticmethod
    def on_any_event(event):
        if event.is_directory:
            return None

        elif event.event_type == 'created':
            # Take any action here when a file is first created.
            log_debug("Received create event - " +  event.src_path)
            Handler.handle_file(event.src_path)


    @staticmethod
    def wait_for_file_write_complete(path):
        ct_size = -1

        while ct_size != os.path.getsize(path):
            ct_size = os.path.getsize(path)
            time.sleep(2)

        time.sleep(2)
        if ct_size != os.path.getsize(path):
            raise Exception("Dump file creation is too slow: " + path)

        log_debug("File write complete - " +  path)


    @staticmethod
    def handle_file(path):

        Handler.wait_for_file_write_complete(path)

        lpath = "/".join(cwd)
        make_new_dir(lpath)
        os.chdir(lpath)

        # Create a new archive with core & more.
        metafiles = cfg.get_dict()["metadata_files_in_archive"]

        fname = os.path.basename(path)
        tarf_name = fname + ".tar.gz"

        cfg.get_core_info(path, hostname)
        
        tar = tarfile.open(tarf_name, "w:gz")
        for e in metafiles:
            tar.add(metafiles[e])
        tar.add(path)
        tar.close()
        log_debug("Tar file for upload created: " + tarf_name)

        Handler.upload_file(tarf_name, tarf_name)

        log_debug("File uploaded - " +  path)
        os.chdir(INIT_CWD)

    @staticmethod
    def upload_file(fname, fpath):
        daemonname = fname.split(".")[0]
        i = 0
        fail_msg = ""
        
        while i <= MAX_RETRIES:
            try:
                svc = FileService(account_name=acctname, account_key=acctkey)

                l = [sonicversion, asicname, daemonname, hostname]
                e = []
                while len(e) != len(l):
                    e.append(l[len(e)])
                    svc.create_directory(sharename, "/".join(e))

                log_debug("Remote dir created: " + "/".join(e))

                svc.create_file_from_path(sharename, "/".join(l), fname, fpath)
                log_debug("Remote file created: name{} path{}".format(fname, fpath))
                break

            except Exception as e:
                log_err("core uploader failed: Failed during upload (" + str(e) +")")
                fail_msg = str(e)
                i += 1
                if i >= MAX_RETRIES:
                    raise Exception("Failed while uploading. msg(" + fail_msg + ") after " + str(i) + " retries")
                time.sleep(PAUSE_ON_FAIL)


    @staticmethod
    def scan():
        for e in os.listdir(CORE_FILE_PATH):
            fl = CORE_FILE_PATH + e
            if os.path.isfile(fl):
                Handler.handle_file(fl)


if __name__ == '__main__':
    try:
        Handler.init()
        w = Watcher()
        Handler.scan()
        w.run()
    except Exception as e:
        log_err("core uploader failed: " + str(e) + " Exiting ...")
    
