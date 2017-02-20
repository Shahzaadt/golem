from __future__ import division

import atexit
import logging
import os
import re
import requests
import subprocess
import time
from datetime import datetime
from os import path
from distutils.version import StrictVersion

import psutil

from devp2p.crypto import privtopub
from ethereum.keys import privtoaddr
from ethereum.transactions import Transaction
from ethereum.utils import normalize_address, denoms

from golem.environments.utils import find_program
from golem.utils import find_free_net_port
from golem.core.compress import save
from golem.core.simpleenv import get_local_datadir

log = logging.getLogger('golem.ethereum')


def ropsten_faucet_donate(addr):
    addr = normalize_address(addr)
    URL_TEMPLATE = "http://faucet.ropsten.be:3001/donate/{}"
    request = URL_TEMPLATE.format(addr.encode('hex'))
    response = requests.get(request)
    if response.status_code != 200:
        log.error("Ropsten Faucet error code {}".format(response.status_code))
        return False
    response = response.json()
    if response['paydate'] == 0:
        log.warning("Ropsten Faucet warning {}".format(response['message']))
        return False
    # The paydate is not actually very reliable, usually some day in the past.
    paydate = datetime.fromtimestamp(response['paydate'])
    amount = int(response['amount']) / denoms.ether
    log.info("Ropsten Faucet: {:.6f} ETH on {}".format(amount, paydate))
    return True


class Faucet(object):
    PRIVKEY = "{:32}".format("Golem Faucet")
    PUBKEY = privtopub(PRIVKEY)
    ADDR = privtoaddr(PRIVKEY)

    @staticmethod
    def gimme_money(ethnode, addr, value):
        nonce = ethnode.get_transaction_count('0x' + Faucet.ADDR.encode('hex'))
        addr = normalize_address(addr)
        tx = Transaction(nonce, 1, 21000, addr, value, '')
        tx.sign(Faucet.PRIVKEY)
        h = ethnode.send(tx)
        log.info("Faucet --({} ETH)--> {} ({})".format(value / denoms.ether,
                                                       '0x' + addr.encode('hex'), h))
        h = h[2:].decode('hex')
        return h

    @staticmethod
    def deploy_contract(ethnode, init_code):
        nonce = ethnode.get_transaction_count(Faucet.ADDR.encode('hex'))
        tx = Transaction(nonce, 0, 3141592, to='', value=0, data=init_code)
        tx.sign(Faucet.PRIVKEY)
        ethnode.send(tx)
        return tx.creates


class NodeProcess(object):
    MIN_GETH_VERSION = '1.4.5'
    MAX_GETH_VERSION = '1.5.999'

    def __init__(self, nodes, datadir):
        self.port = None
        self.__prog = find_program('geth')
        if not self.__prog:
            raise OSError("Ethereum client 'geth' not found")
        output, _ = subprocess.Popen([self.__prog, 'version'],
                                     stdout=subprocess.PIPE).communicate()
        ver = StrictVersion(re.search("Version: (\d+\.\d+\.\d+)", output).group(1))
        if ver < self.MIN_GETH_VERSION or ver > self.MAX_GETH_VERSION:
            raise OSError("Incompatible Ethereum client 'geth' version: {}".format(ver))
        log.info("geth version {}".format(ver))

        if not path.exists(datadir):
            os.makedirs(datadir)
        if not path.isdir(datadir):
            raise IOError("{} does not exist or is not a dir".format(datadir))

        if nodes:
            nodes_file = path.join(datadir, 'static-nodes.json')
            save(nodes, nodes_file, False)

        # Init the ethereum node with genesis block information.
        # Do it always to overwrite invalid genesis block information
        # (e.g. genesis of main Ethereum network)
        genesis_file = path.join(path.dirname(__file__),
                                 'genesis_golem.json')
        init_args = [self.__prog, '--datadir', datadir, 'init', genesis_file]
        subprocess.check_call(init_args)
        log.info("geth init: {}".format(' '.join(init_args)))

        self.datadir = datadir
        self.__ps = None
        self.rpcport = None

    def is_running(self):
        return self.__ps is not None

    def start(self, rpc, mining=False, nodekey=None, port=None):
        if self.__ps:
            return

        if not port:
            port = find_free_net_port()

        self.port = port
        args = [
            self.__prog,
            '--datadir', self.datadir,
            '--networkid', '9',
            '--port', str(self.port),
            '--nodiscover',
            '--ipcdisable',  # Disable IPC transport - conflicts on Windows.
            '--gasprice', '0',
            '--verbosity', '3',
        ]

        if rpc:
            self.rpcport = find_free_net_port()
            args += [
                '--rpc',
                '--rpcport', str(self.rpcport)
            ]

        if nodekey:
            self.pubkey = privtopub(nodekey)
            args += [
                '--nodekeyhex', nodekey.encode('hex'),
            ]

        if mining:
            mining_script = path.join(path.dirname(__file__),
                                      'mine_pending_transactions.js')
            args += [
                '--etherbase', Faucet.ADDR.encode('hex'),
                'js', mining_script,
            ]

        self.__ps = psutil.Popen(args, close_fds=True)
        atexit.register(lambda: self.stop())
        WAIT_PERIOD = 0.01
        wait_time = 0
        while True:
            # FIXME: Add timeout limit, we don't want to loop here forever.
            time.sleep(WAIT_PERIOD)
            wait_time += WAIT_PERIOD
            if not self.rpcport:
                break
            if self.rpcport in set(c.laddr[1] for c
                                   in self.__ps.connections('tcp')):
                break
        log.info("Node started in {} s: `{}`".format(wait_time, " ".join(args)))

    def stop(self):
        if self.__ps:
            start_time = time.clock()

            try:
                self.__ps.terminate()
                self.__ps.wait()
            except psutil.NoSuchProcess:
                log.warn("Cannot terminate node: process {} no longer exists".format(self.__ps.pid))

            self.__ps = None
            self.rpcport = None
            duration = time.clock() - start_time
            log.info("Node terminated in {:.2f} s".format(duration))


class FullNode(NodeProcess):
    def __init__(self, datadir=None, run=True):
        if not datadir:
            datadir = path.join(get_local_datadir('ethereum'), 'full_node')
        super(FullNode, self).__init__(nodes=[], datadir=datadir)
        if run and not self.is_running():
            self.start(rpc=False, mining=True, nodekey=Faucet.PRIVKEY, port=30900)

if __name__ == "__main__":
    import signal
    import sys

    logging.basicConfig(level=logging.INFO)
    FullNode()

    # The best I have to make the node running untill interrupted.
    def handler(*unused):
        sys.exit()
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    while True:
        time.sleep(60 * 60 * 24)
