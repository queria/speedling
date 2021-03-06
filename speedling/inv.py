import ipaddress
import random

from speedling import control
from speedling import facility
import logging

from collections import abc
from speedling import conf

from osinsutils import netutils
from osinsutils import localsh

LOG = logging.getLogger(__name__)


ALL_NODE_DATA = {}
INVENTORY = {}
ALL_NODES = set()
THIS_NODE = set()  # empty or single element

HOST_WITH_SERVICE_CACHE = {}


def hosts_with_service(service):
    if service in HOST_WITH_SERVICE_CACHE:
        return HOST_WITH_SERVICE_CACHE[service]
    related = set()
    for n, node in INVENTORY.items():
        if 'services' in node and service in node['services']:
            related.add(n)
    HOST_WITH_SERVICE_CACHE[service] = related
    return related


HOST_WITH_COMPONENT_CACHE = {}


def hosts_with_component(component):
    if component in HOST_WITH_COMPONENT_CACHE:
        return HOST_WITH_COMPONENT_CACHE[component]
    related = set()
    for n, node in INVENTORY.items():
        if 'components' in node and component in node['components']:
            related.add(n)

    c = facility.get_component(component)
    if 'services' in c and c['services']:
        related.update(hosts_with_any_service(list(c['services'].keys())))
    HOST_WITH_COMPONENT_CACHE[component] = related
    return related


def hosts_with_any_service(services):
    l = len(services)
    if not l:
        return set()
    if l == 1:
        return hosts_with_service(services[0])
    ckey = tuple(sorted(services))
    if ckey in HOST_WITH_SERVICE_CACHE:
        return HOST_WITH_SERVICE_CACHE[ckey]
    allsrv = set.union(*[hosts_with_service(s) for s in services])
    HOST_WITH_SERVICE_CACHE[ckey] = allsrv
    return allsrv


def inventory_set_local_node(inv_name):
    assert not THIS_NODE
    assert inv_name in ALL_NODES
    THIS_NODE.add(inv_name)


def inventory_register_node(inv_name, inv_data):
    inv_data['inventory_name'] = inv_name
    node = {'inv': inv_data,
            'keys': {},
            'peers': {}}
    ALL_NODE_DATA[inv_name] = node
    INVENTORY[inv_name] = inv_data
    ALL_NODES.add(inv_name)


def get_node(inv_name):
    return ALL_NODE_DATA[inv_name]


ENABLED_SRV_CACHE = None


def get_enabled_services():
    global ENABLED_SRV_CACHE
    if ENABLED_SRV_CACHE is None:
        ENABLED_SRV_CACHE = set.union(*[node['services'] for n, node in INVENTORY.items() if 'services' in node])
    return ENABLED_SRV_CACHE


def rand_pick(hosts, k=1):
    return set(random.sample(hosts, k))


def check_response(func, response):
    f_name = control.func_to_str(func)
    failure = []
    for h, r in response.items():
        if r['status'] != 0:
            failure += h
    if failure:
        e = ('Failure on non zero with {do} node: {resp}'.format(
             do=f_name,
             resp=str(response)))
        raise Exception(e)


def _call_str_fun(fun, c_args, c_kwargs):
    if isinstance(fun, abc.Callable):
        fun(*c_args, **c_kwargs)
    else:
        arg_str = '('
        if c_args:
            arg_str += '*' + str(c_args)
        if c_kwargs:
            if c_args:
                arg_str += ', '
            arg_str += '**' + str(c_kwargs)
        arg_str += ')'
        eval(fun + arg_str)


# c_args, cw_args does not overlaps with others
# thes function ment to be used without args in the 98% of tha case
def do_do(hosts, the_do, c_args=tuple(), c_kwargs={}):
    remote_hosts = hosts - THIS_NODE
    do_local = True if THIS_NODE.intersection(hosts) else False
    task_id = control.call_function(remote_hosts, the_do, c_args, c_kwargs)
    local_succed = True
    ex = None
    try:
        if do_local:
            rv = _call_str_fun(the_do, c_args, c_kwargs)
    except Exception as e:
        ex = e
        local_succed = False
        LOG.exception(control.func_to_str(the_do))
    response = control.wait_for_all_response(task_id)
    if do_local:
        assert len(THIS_NODE) == 0
        response[next(iter(THIS_NODE))] = {'status': 0, 'return_value': rv}
    check_response(the_do, response)
    if not local_succed:
        raise ex
    return response


THIS_NODE_INV = None
THIS_NODE_ALL = None


def get_this_inv():
    assert THIS_NODE_INV
    return THIS_NODE_INV


def get_this_node():
    assert THIS_NODE_ALL
    return THIS_NODE_ALL


def do_set_identity(node_data, global_data):
    global GLOBAL_CONFIG
    global THIS_NODE_INV
    global THIS_NODE_ALL
    # register node
    THIS_NODE_INV = node_data['inv']
    # register creds
    # register peers
    # register global config
    THIS_NODE_ALL = node_data
    conf.GLOBAL_CONFIG = global_data


def load_allocations():
    raise NotImplementedError


def save_allocations():
    raise NotImplementedError


# prefered_familiy, ipv6, ipv4, ipv6+ipv4, ib
def allocate_for(inv, network, prefered_familiy):
    raise NotImplementedError
    return set()


PSEUDO_ADDRESSES = {'default_gw', 'sshed_address'}


def address_with_porpouse(inv, glob_net_def, porpouse, pseudo=False):
    if 'networks' not in inv:
        return
    addrs = set()

    for n, net in inv['networks'].items():
        if 'porpouse' in net:
            p = net['porpouse']
        else:
            p = set()
        if n in glob_net_def:
            p += glob_net_def.get('porpouse', set())
        if porpouse in p:
            if 'addresses' in net:
                addrs += net['addresses']

    if not pseudo:
        addrs -= PSEUDO_ADDRESSES
    return addrs


PSEUDO_DICT = {}


def _chase_ssh_address():
    raise NotImplementedError


def is_ipaddr(addr):
    try:
        ipaddress.IPv4Address(addr)
        return True
    except:
        pass
    try:
        ipaddress.IPv6Address(addr)
        return True
    except:
        pass
    return False


# hackish dev env tricks
# probably it wil be removed
def resolve_pseuodo_local(key):
    if key in PSEUDO_DICT:
        return PSEUDO_DICT[key]
    inv = THIS_NODE_INV
    # not it maybe hostname or ssh conf name
    # to really figure out we need to find the connected socket
    if key == 'sshed_address':
        ssh_address = inv['ssh_address']
        if not is_ipaddr(ssh_address):
            ssh_address = _chase_ssh_address()
        PSEUDO_DICT['sshed_address'] = ssh_address
        return ssh_address
    elif key == 'default_gw':  # _local_ address used for reaching out
        cand = netutils.discover_default_route_src_addr()
        if is_ipaddr(cand):
            PSEUDO_DICT['default_gw'] = cand
            return cand
        raise ValueError('{cand} is not an ip address'.format(cand=cand))
    return NotImplemented(key)


def address_return(pourpose, addresses):
    if not addresses:
        raise Exception('Zero address for {pourpose}'.format(pourpose=pourpose))
    selected = next(iter(addresses))
    if len(addresses) > 1:
        LOG.warning('Multiple address found for {pourpose} picking {addr} from {addrs}'.format(addr=selected,
                    addrs=str(addresses), pourpose=pourpose))
    return selected


def get_addr_for(inv, pourpose, service=None, component=None, net_attr=None):
    # TODO: in strict validate mode check is the configured net
    #       realy ment for the pourpose
    fallback_mode = []
    if service:
        if net_attr in service:
            return address_return(pourpose, inv['networks'][service[net_attr]]['addresses'])
        if 'component' in service:
            component = service['component']
    if component:
        if net_attr in component:
            return address_return(pourpose, inv['networks'][component[net_attr]]['addresses'])
    glob_net_def = conf.get_global_nets()
    addresses = address_with_porpouse(inv, glob_net_def, pourpose)
    if addresses:
        return address_return(pourpose, addresses)
    fallback_mode = conf.get_global_config().get('allow_address_fallback', [])
    if fallback_mode and THIS_NODE_INV is not inv:
        if 'sshed_address' in fallback_mode:
            ssh_address = inv['ssh_address']
            if is_ipaddr(ssh_address):
                return ssh_address
        raise NotImplementedError('other mode not implemented for non local usage')
    for t in fallback_mode:
        try:
            address = resolve_pseuodo_local(t)
            return address
        except Exception as e:
            LOG.debug(e)
    raise NotImplementedError('No more way to get address')


# call after the inventory is fully populated
# if fills the missing netinfo
# it may allocate addresses which needs to be saved in the state dir
def process_net():
    glob_net_def = conf.get_global_nets()

    for name, node in INVENTORY.items():
        if 'networks' in node:
            for n, net in node['networks'].items():
                net['name'] = n
                # extend with global porpouse?
                if 'addresses' not in net:
                    net['addresses'] = {}
                if not net['addresses'] and 'allocate_for' in net:
                    allocate_for(node, n, allocate_for)
                if 'no_l3_address' not in net and not net['addresses']:
                    raise ValueError("No address for net '{net}' on host '{host}'".format(host=name, net=n))
        ssh_address = node.get('ssh_address', None)
        if not ssh_address:
            strategy = node.get('default_ssh_address_strategy', 'inventory')
            if strategy == 'inventory':
                node['ssh_address'] = name
            elif strategy == 'sshnet':
                addrs = address_with_porpouse(node, glob_net_def, 'sshnet')
                if not addrs:
                    raise ValueError('No address for {node} for porpouse sshnet'.format(node=name))
                ssh_address = next(iter(addrs))
                if len(addrs) > 1:
                    LOG.warning('Multiple address found for ssh picking {addr} from {addrs}'.format(addr=ssh_address, addrs=str(addrs)))
                node['ssh_address'] = ssh_address
            else:
                raise NotImplementedError("Unknown ssh strategy: {stra}".format(stra=strategy))


def distribute_as_file(hosts, content, path, owner='root', group='root', mode=0o400):
    sf = control.StreamFactoryBytes(content)
    kwargs = {'path': path,
              'mode': mode,
              'owner': owner,
              'group': group}
    func = 'speedling.receiver.file_writer'
    task_id = control.call_function_stream(hosts, sf, func, c_kwargs=kwargs)
    response = control.wait_for_all_response(task_id)
    check_response(func, response)


def cmd_stream(*args, **kwrags):
    return localsh.run_stream_in(*args, **kwrags)


def distribute_for_command(hosts, content, cmd):
    sf = control.StreamFactoryBytes(content)
    kwargs = {'cmd': cmd}
    func = cmd_stream
    task_id = control.call_function_stream(hosts, sf, func, c_kwargs=kwargs)
    response = control.wait_for_all_response(task_id)
    check_response(func, response)


# TODO: dropping local thing ???
def do_diff(matrix, do):
    do_local = False
    local_parms = {}
    if THIS_NODE:
        local_node = next(iter(THIS_NODE))
        if local_node in matrix.keys():
            do_local = True
            local_parms = matrix[local_node]
            del matrix[local_node]

    task_id = control.call_function_diff(matrix, do)
    if do_local:
        return_value = do(*local_parms.get('args', tuple()), **local_parms.get('kwargs', {}))
    response = control.wait_for_all_response(task_id)
    if do_local:
        # exception already raised if status != 0
        response[local_node] = {'status': 0, 'return_value': return_value}
    check_response(do, response)
    return response


def set_identity():
    glb_config = conf.get_global_config()
    msg_matrix = {}
    for h in ALL_NODES:
        msg_matrix[h] = {'kwargs': {'node_data': ALL_NODE_DATA[h],
                                    'global_data': glb_config}}
    do_diff(msg_matrix, do_set_identity)
