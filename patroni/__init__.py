import logging
import signal
import sys
import time

from patroni.api import RestApiServer
from patroni.config import Config
from patroni.dcs import get_dcs
from patroni.exceptions import DCSError
from patroni.ha import Ha
from patroni.postgresql import Postgresql
from patroni.utils import reap_children, sigchld_handler
from patroni.version import __version__

logger = logging.getLogger(__name__)


class Patroni(object):

    def __init__(self):
        self.setup_signal_handlers()

        self.version = __version__
        self.config = Config()
        self.dcs = get_dcs(self.config)
        self.load_dynamic_configuration()

        self.postgresql = Postgresql(self.config['postgresql'])
        self.api = RestApiServer(self, self.config['restapi'])
        self.ha = Ha(self)

        self.tags = self.get_tags()
        self.next_run = time.time()
        self.scheduled_restart = {}

    def load_dynamic_configuration(self):
        while True:
            try:
                cluster = self.dcs.get_cluster()
                if cluster and cluster.config:
                    if self.config.set_dynamic_configuration(cluster.config):
                        self.dcs.reload_config(self.config)
                elif not self.config.dynamic_configuration and 'bootstrap' in self.config:
                    if self.config.set_dynamic_configuration(self.config['bootstrap']['dcs']):
                        self.dcs.reload_config(self.config)
                break
            except DCSError:
                logger.warning('Can not get cluster from dcs')

    def get_tags(self):
        return {tag: value for tag, value in self.config.get('tags', {}).items()
                if tag not in ('clonefrom', 'nofailover', 'noloadbalance') or value}

    @property
    def nofailover(self):
        return bool(self.tags.get('nofailover', False))

    def reload_config(self):
        try:
            self.tags = self.get_tags()
            self.dcs.reload_config(self.config)
            self.api.reload_config(self.config['restapi'])
            self.postgresql.reload_config(self.config['postgresql'])
        except Exception:
            logger.exception('Failed to reload config_file=%s', self.config.config_file)

    @property
    def replicatefrom(self):
        return self.tags.get('replicatefrom')

    def sighup_handler(self, *args):
        self._received_sighup = True

    def sigterm_handler(self, *args):
        if not self._received_sigterm:
            self._received_sigterm = True
            sys.exit()

    @property
    def noloadbalance(self):
        return bool(self.tags.get('noloadbalance', False))

    def schedule_next_run(self):
        self.next_run += self.dcs.loop_wait
        current_time = time.time()
        nap_time = self.next_run - current_time
        if nap_time <= 0:
            self.next_run = current_time
            # Release the GIL so we don't starve anyone waiting on async_executor lock
            time.sleep(0.001)
            # Warn user that Patroni is not keeping up
            logger.warning("Loop time exceeded, rescheduling immediately.")
        elif self.dcs.watch(nap_time):
            self.next_run = time.time()

    def run(self):
        self.api.start()
        self.next_run = time.time()

        while not self._received_sigterm:
            if self._received_sighup:
                self._received_sighup = False
                if self.config.reload_local_configuration():
                    self.reload_config()

            logger.info(self.ha.run_cycle())

            cluster = self.dcs.cluster
            if cluster and cluster.config and self.config.set_dynamic_configuration(cluster.config):
                self.reload_config()

            if not self.postgresql.data_directory_empty():
                self.config.save_cache()

            reap_children()
            self.schedule_next_run()

    def setup_signal_handlers(self):
        self._received_sighup = False
        self._received_sigterm = False
        signal.signal(signal.SIGHUP, self.sighup_handler)
        signal.signal(signal.SIGTERM, self.sigterm_handler)
        signal.signal(signal.SIGCHLD, sigchld_handler)


def main():
    logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s', level=logging.INFO)
    logging.getLogger('requests').setLevel(logging.WARNING)

    patroni = Patroni()
    try:
        patroni.run()
    except KeyboardInterrupt:
        pass
    finally:
        patroni.api.shutdown()
        if patroni.ha.is_paused():
            logger.info('Leader key is not deleted and Postgresql is not stopped due paused state')
        else:
            patroni.postgresql.stop(checkpoint=False)
            patroni.dcs.delete_leader()
