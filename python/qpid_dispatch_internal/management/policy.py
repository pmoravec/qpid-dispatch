#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License
#

"""
Utilities for command-line programs.
"""

import sys, optparse, os
import ConfigParser
from collections import Sequence, Mapping, namedtuple
from qpid_dispatch_site import VERSION
import pdb #; pdb.set_trace()
import ast
import socket
import binascii



"""
Entity implementing the business logic of user connection/access policy.

Policy is represented several ways:

1. External       : ConfigParser-format file
2. CRUD Interface : ConfigParser file section: name, [(name, value), ...]
3. Internal       : dictionary

For example:

1. External

The External Policy is a plain ascii text file formatted for processing
by ConfigParser.

External Policy:
----------------

    [photoserver]
    schemaVersion            : 1
    policyVersion            : 1
    roles: {
      'users'           : ['u1', 'u2'],
      'paidsubscribers' : ['p1', 'p2']
      }

2. CRUD Interface

At the CRUD Create function the policy is represented by two strings:
- name : name of the ConfigParser section
- data : ConfigParser section as a string

The CRUD Interface policy is created by ConfigParser.read(externalFile)
and then iterating through the config parser sections.

CRUD Interface Policy:
----------------------

    'photoserver', '[('schemaVersion', '1'), 
                     ('policyVersion', '1'), 
                     ('roles', "{\n
                       'users'           : ['u1', 'u2'],\n
                       'paidsubscribers' : ['p1', 'p2']\n}")]'

3. Internal

Internally the policy is stored in a python dictionary. 
Policies are converted from CRUD Interface format to Internal format
by a compilation phase. The compiler sanitizes the input and
creates the nested structures needed for run-time processing.

Internal Policy:
----------------

    data['photoserver'] = 
    {'schemaVersion': 1, 
     'roles': {'paidsubscribers': ['p1', 'p2'], 
               'users': ['u1', 'u2']}, 
     'policyVersion': 1}

"""

#
#
class PolicyError(Exception):
    def __init__(self, value):
        self.value = value
    def __str__(self):
        return repr(self.value)

#
#
class HostStruct():
    """
    HostStruct represents a single, binary socket address from getaddrinfo
        - name     : name given to constructor, numeric IP or host name
        - saddr    : net name resolved by getaddrinfo, numeric IP
        - family   : saddr.family, int
        - binary   : saddr packed binary address, binary string
    """
    families = [socket.AF_INET]
    famnames = ["IPv4"]
    if socket.has_ipv6:
        families.append(socket.AF_INET6)
        famnames.append("IPv6")

    def __init__(self, hostname):
        """
        Given a host name text string, return the socket info for it.
        @param[in] hostname host IP address to parse
        """
        try:
            res = socket.getaddrinfo(hostname, 0)
            if len(res) == 0:
                raise PolicyError("HostStruct: '%s' did not resolve to an IP address" % hostname)
            foundFirst = False
            saddr = ""
            sfamily = socket.AF_UNSPEC
            for i0 in range(0, len(res)):
                family, dum0, dum1, dum2, sockaddr = res[i0]
                if not foundFirst:
                    if family in self.families:
                        saddr = sockaddr[0]
                        sfamily = family
                        foundFirst = True
                else:
                    if family in self.families:
                        if not saddr == sockaddr[0] or not sfamily == family:
                            raise PolicyError("HostStruct: '%s' resolves to multiple IP addresses" %
                                              hostname)

            if not foundFirst:
                raise PolicyError("HostStruct: '%s' did not resolve to one of the supported address family" %
                        hostname)
            self.name = hostname
            self.saddr = saddr
            self.family = sfamily
            self.binary = socket.inet_pton(family, saddr)
            return
        except Exception, e:
            raise PolicyError("HostStruct: '%s' failed to resolve: '%s'" %
                              (hostname, e))

    def __str__(self):
        return self.name

    def __repr__(self):
        return self.__str__()

    def dump(self):
        return ("(%s, %s, %s, %s)" %
                (self.name,
                 self.saddr,
                 "AF_INET" if self.family == socket.AF_INET else "AF_INET6",
                 binascii.hexlify(self.binary)))

#
#
class HostAddr():
    """
    Provide HostIP address ranges and comparison functions.
    A HostIP may be:
    - single address:      10.10.1.1
    - a pair of addresses: 10.10.0.0,10.10.255.255
    Only IPv4 and IPv6 are supported.
    - No unix sockets.
    HostIP names must resolve to a single IP address.
    Address pairs define a range.
    - The second address must be numerically larger than the first address.
    - The addresses must be of the same address 'family', IPv4 or IPv6.
    IPv6 support is conditional based on underlying OS network options.
    Raises a PolicyError on validation error in constructor.
    """

    def has_ipv6(self):
        return socket.has_ipv6

    def __init__(self, hostspec):
        """
        Parse host spec into binary structures to use for comparisons.
        Validate the hostspec to enforce usage rules.
        """
        self.hoststructs = []

        if hostspec == "*":
            self.wildcard = True
        else:
            self.wildcard = False

            hosts = [x.strip() for x in hostspec.split(",")]

            # hosts must contain one or two host specs
            if len(hosts) not in [1, 2]:
                raise PolicyError("hostspec must contain 1 or 2 host names")
            self.hoststructs.append(HostStruct(hosts[0]))
            if len(hosts) > 1:
                self.hoststructs.append(HostStruct(hosts[1]))
                if not self.hoststructs[0].family == self.hoststructs[1].family:
                    raise PolicyError("mixed IPv4 and IPv6 host specs in range not allowed")
                c0 = self.memcmp(self.hoststructs[0].binary, self.hoststructs[1].binary)
                if c0 > 0:
                    raise PolicyError("host specs in range must have lower numeric address first")

    def __str__(self):
        if self.wildcard:
            return "*"
        res = self.hoststructs[0].name
        if len(self.hoststructs) > 1:
            res += "," + self.hoststructs[1].name
        return res

    def __repr__(self):
        return self.__str__()

    def dump(self):
        if self.wildcard:
            return "(*)"
        res = "(" + self.hoststructs[0].dump()
        if len(self.hoststructs) > 1:
            res += "," + self.hoststructs[1].dump()
        res += ")"
        return res

    def memcmp(self, a, b):
        res = 0
        for i in range(0,len(a)):
            if a[i] > b[i]:
                res = 1
                break;
            elif a[i] < b[i]:
                res = -1
                break
        return res

    def match_bin(self, cstruct):
        """
        Does the candidate hoststruct match the IP or range of IP addresses represented by this?
        @param[in] cstruct the IP address to be tested
        @return candidate matches this or not
        """
        if self.wildcard:
            return True
        try:
            if not cstruct.family == self.hoststructs[0].family:
                # sorry, wrong AF_INET family
                return False
            c0 = self.memcmp(cstruct.binary, self.hoststructs[0].binary)
            if len(self.hoststructs) == 1:
                return c0 == 0
            c1 = self.memcmp(cstruct.binary, self.hoststructs[1].binary)
            return c0 >= 0 and c1 <= 0
        except PolicyError:
            return False
        except Exception, e:
            assert isinstance(cstruct, HostStruct), \
                ("Wrong type. Expected HostStruct but received %s" % cstruct.__class__.__name__)
            return False

    def match_str(self, candidate):
        """
        Does the candidate string match the IP or range represented by this?
        @param[in] candidate the IP address to be tested
        @return candidate matches this or not
        """
        try:
            hoststruct = HostStruct(candidate)
        except PolicyError:
            return False
        return self.match_bin(hoststruct)


#
#
class PolicyKeys():
    # Internal policy key words
    KW_POLICY_VERSION = "policyVersion"
    KW_SCHEMA_VERSION = "schemaVersion"



#
#
class PolicyCompiler():
    """
    Compile CRUD Interface policy into Internal format.
    Validate incoming configuration for legal schema.
    - Warn about section options that go unused.
    - Disallow negative max connection numbers.
    - Check that connectionOrigins resolve to IP hosts
    """
    schema_version = 1

    schema_allowed_options = [(), (
        'connectionAllowUnrestricted',
        'connectionOrigins',
        'connectionPolicy',
        'maximumConnections',
        'maximumConnectionsPerHost',
        'maximumConnectionsPerUser',
        'policies',
        PolicyKeys.KW_POLICY_VERSION,
        'roles',
        PolicyKeys.KW_SCHEMA_VERSION)
        ]
    schema_disallowed_options = [(),
        ()
        ]

    allowed_opts = ()
    disallowed_opts = ()
    crud_compiler_fn = None


    def __init__(self, schema_version=1):
        """
        Create a validator for the given schema version.
        @param[in] schema_version version selector
        """
        if schema_version == 1:
            self.crud_compiler_fn = self.crud_compiler_v1
        else:
            raise PolicyError(
                "Illegal policy schema version %s. Must be '1'." % schema_version)
        self.schema_version  = schema_version
        self.allowed_opts    = self.schema_allowed_options[schema_version]
        self.disallowed_opts = self.schema_disallowed_options[schema_version]


    def validateNumber(self, val, v_min, v_max, errors):
        """
        Range check a numeric int policy value
        @param[in] val policy value to check
        @param[in] v_min minumum value
        @param[in] v_max maximum value. zero disables check
        @param[out] errors failure message
        @return v_min <= val <= v_max
        """
        error = ""
        try:
            v_int = int(val)
        except Exception, e:
            errors.append("Value '%s' does not resolve to an integer." % val)
            return False
        if v_int < v_min:
            errors.append("Value '%s' is below minimum '%s'." % (val, v_min))
            return False
        if v_max > 0 and v_int > v_max:
            errors.append("Value '%s' is above maximum '%s'." % (val, v_max))
            return False
        return True


    def crud_compiler_v1_origins(self, name, submap, warnings, errors):
        """
        Handle an origins submap from a CRUD Interface request.
        Each origin value is verified. On a successful run the submap
        is replaced parsed lists of HostAddr objects.
        @param[in] name application name
        @param[in] submap CRUD Interface policy
        @param[out] warnings nonfatal irregularities observed
        @param[out] errors descriptions of failure
        @return - origins is usable. If True then warnings[] may contain useful
                  information about fields that are ignored. If False then
                  warnings[] may contain info and errors[0] will hold the
                  description of why the origin was rejected.
        """
        key = "connectionOrigins"
        newmap = {}
        for coname in submap:
            try:
                olist = submap[coname]
                if not type(olist) is list:
                    errors.append("Application '%s' option '%s' connectionOption '%s' must be type 'list' but is '%s'." %
                                    (name, key, coname, type(olist)))
                    return False
                newmap[coname] = []
                for co in olist:
                    coha = HostAddr(co)
                    newmap[coname].append(coha)
            except Exception, e:
                errors.append("Application '%s' option '%s' connectionOption '%s' failed to translate: '%s'." %
                                (name, key, coname, e))
                return False
        submap.update(newmap)
        return True


    def crud_compiler_v1_policies(self, name, submap, warnings, errors):
        """
        Handle a policies submap from a CRUD Interface request.
        Validates policy only returning warnings and errors. submap is unchanged
        @param[in] name application name
        @param[in] submap CRUD Interface policy
        @param[out] warnings nonfatal irregularities observed
        @param[out] errors descriptions of failure
        @return - policy is usable. If True then warnings[] may contain useful
                  information about fields that are ignored. If False then
                  warnings[] may contain info and errors[0] will hold the
                  description of why the policy was rejected.
        """
        key = "policies"
        cerror = []
        for pname in submap:
            for setting in submap[pname]:
                sval = submap[pname][setting]
                if setting in ['max_frame_size',
                               'max_message_size',
                               'max_receivers',
                               'max_senders',
                               'max_session_window',
                               'max_sessions'
                               ]:
                    if not self.validateNumber(sval, 0, 0, cerror):
                        errors.append("Application '%s' option '%s' policy '%s' setting '%s' has error '%s'." %
                                      (name, key, pname, setting, cerror[0]))
                        return False
                elif setting in ['allow_anonymous_sender',
                                 'allow_dynamic_src'
                                 ]:
                    if not type(sval) is bool:
                        errors.append("Application '%s' option '%s' policy '%s' setting '%s' has illegal boolean value '%s'." %
                                      (name, key, pname, setting, sval))
                        return False
                elif setting in ['sources',
                                 'targets'
                                 ]:
                    if not type(sval) is list:
                        errors.append("Application '%s' option '%s' policy '%s' setting '%s' must be type 'list' but is '%s'." %
                                      (name, key, pname, setting, type(sval)))
                        return False
                else:
                    warnings.append("Application '%s' option '%s' policy '%s' setting '%s' is ignored." %
                                     (name, key, pname, setting))
        return True


    def crud_compiler_v1(self, name, policy_in, policy_out, warnings, errors):
        """
        Compile a schema from CRUD format to Internal format.
        @param[in] name application name
        @param[in] policy_in CRUD Interface policy
        @param[out] policy_out validated Internal format
        @param[out] warnings nonfatal irregularities observed
        @param[out] errors descriptions of failure
        @return - policy is usable. If True then warnings[] may contain useful
                  information about fields that are ignored. If False then
                  warnings[] may contain info and errors[0] will hold the
                  description of why the policy was rejected.
        """
        cerror = []
        # validate the options
        for (key, val) in policy_in:
            if key not in self.allowed_opts:
                warnings.append("Application '%s' option '%s' is ignored." %
                                (name, key))
            if key in self.disallowed_opts:
                errors.append("Application '%s' option '%s' is disallowed." %
                              (name, key))
                return False
            if key == PolicyKeys.KW_SCHEMA_VERSION:
                if not int(self.schema_version) == int(val):
                    errors.append("Application '%s' expected schema version '%s' but is '%s'." %
                                  (name, self.schema_version, val))
                    return False
                policy_out[key] = val
            if key == PolicyKeys.KW_POLICY_VERSION:
                if not self.validateNumber(val, 0, 0, cerror):
                    errors.append("Application '%s' option '%s' must resolve to a positive integer: '%s'." %
                                    (name, key, cerror[0]))
                    return False
                policy_out[key] = val
            elif key in ['maximumConnections',
                         'maximumConnectionsPerHost',
                         'maximumConnectionsPerUser'
                         ]:
                if not self.validateNumber(val, 0, 65535, cerror):
                    msg = ("Application '%s' option '%s' has error '%s'." % 
                           (name, key, cerror[0]))
                    errors.append(msg)
                    return False
                policy_out[key] = val
            elif key in ['connectionOrigins',
                         'connectionPolicy',
                         'policies',
                         'roles'
                         ]:
                try:
                    submap = ast.literal_eval(val)
                    if not type(submap) is dict:
                        errors.append("Application '%s' option '%s' must be of type 'dict' but is '%s'" %
                                      (name, key, type(submap)))
                        return False
                    if key == "connectionOrigins":
                        if not self.crud_compiler_v1_origins(name, submap, warnings, errors):
                            return False
                    if key == "policies":
                        if not self.crud_compiler_v1_policies(name, submap, warnings, errors):
                            return False
                    policy_out[key] = submap
                except Exception, e:
                    errors.append("Application '%s' option '%s' error processing map: %s" %
                                  (name, key, e))
                    return False
        return True


class Policy():
    """
    The policy database.
    """

    data = {}
    folder = "."
    schema_version = 1
    policy_compiler = None

    def __init__(self, folder=".", schema_version=1):
        """
        Create instance
        @params folder: relative path from __file__ to conf file folder
        """
        self.folder = folder
        self.schema_version = schema_version
        self.policy_compiler = PolicyCompiler(schema_version)
        self.policy_io_read_files()

    #
    # Policy file I/O
    #
    def policy_io_read_files(self):
        """
        Read all conf files and create the policies they contain.
        """
        apath = os.path.abspath(os.path.dirname(__file__))
        apath = os.path.join(apath, self.folder)
        for i in os.listdir(apath):
            if i.endswith(".conf"):
                self.policy_io_read_file(os.path.join(apath, i))

    def policy_io_read_file(self, fn):
        """
        Read a single policy config file.
        A file may hold multiple policies in separate ConfigParser sections.
        All policies validated before any are committed.
        Create each policy in db.
        @param fn: absolute path to file
        """
        try:
            cp = ConfigParser.ConfigParser()
            cp.optionxform = str
            cp.read(fn)

        except Exception, e:
            raise PolicyError( 
                "Error processing policy configuration file '%s' : %s" % (fn, e))
        newpolicies = {}
        for policy in cp.sections():
            warnings = []
            diag = []
            candidate = {}
            if not self.policy_compiler.crud_compiler_fn(policy, cp.items(policy), candidate, warnings, diag):
                msg = "Policy file '%s' is invalid: %s" % (fn, diag[0])
                raise PolicyError( msg )
            if len(warnings) > 0:
                print ("LogMe: Policy file '%s' application '%s' has warnings: %s" %
                       (fn, policy, warnings))
            newpolicies[policy] = candidate
        # Log a warning if policy from one config file replaces another.
        # TODO: Should this throw?
        for c in newpolicies:
            c_ver = 0
            e_ver = 0
            c_pol = newpolicies[c]
            if PolicyKeys.KW_POLICY_VERSION in c_pol:
                c_ver = int(c_pol[PolicyKeys.KW_POLICY_VERSION])
            if c in self.data:
                e_pol = self.data[c]
                if PolicyKeys.KW_POLICY_VERSION in e_pol:
                    e_ver = int(e_pol[PolicyKeys.KW_POLICY_VERSION])
                if c_ver < e_ver:
                    kw = "downgrades"
                elif c_ver == e_ver:
                    kw = "replaces"
                else:
                    kw = "upgrades"
                msg = ("LogMe: WARNING Policy file '%s' application '%s' policy version '%s' %s existing policy version '%s'." %
                    (fn, c, c_ver, kw, e_ver))
                print msg
        self.data.update(newpolicies)


    #
    # CRUD interface
    #
    def policy_create(self, name, policy):
        """
        Create named policy
        @param name: policy name
        @param policy: policy data
        """
        warnings = []
        diag = []
        candidate = {}
        result = self.policy_compiler.crud_compiler_fn(name, policy, candidate, warnings, diag)
        if not result:
            raise PolicyError( "Policy '%s' is invalid: %s" % (name, diag[0]) )
        if len(warnings) > 0:
            print ("LogMe: Application '%s' has warnings: %s" %
                   (name, warnings))
        self.data[name] = candidate

    def policy_read(self, name):
        """Read named policy"""
        return self.data[name]

    def policy_update(self, name, policy):
        """Update named policy"""
        pass

    def policy_delete(self, name):
        """Delete named policy"""
        del self.data[name]

    #
    # db enumerator
    #
    def policy_db_get_names(self):
        """Return a list of policy names."""
        return self.data.keys()


    #
    # Runtime query interface
    #
    def policy_aggregate_limits(self, upolicy, policy, settingname):
        """
        Force a max count value into user policy
        param[in,out] upolicy user policy receiving aggregations
        param[in] policy Internal policy holding settings to be aggregated
        param[in] settingname setting of interest
        """
        if settingname in policy:
            upolicy[settingname] = policy[settingname]

    def policy_aggregate_policy_int(self, upolicy, policy, roles, settingname):
        """
        Pull int out of policy.policies[role] and install into upolicy if > existing
        param[in,out] upolicy user policy receiving aggregations
        param[in] policy Internal policy holding settings to be aggregated
        param[in] settingname setting of interest
        """
        if not 'policies' in policy:
            return
        policies = policy['policies']
        for role in roles:
            if role in policies:
                rpol = policies[role]
                if settingname in rpol:
                    sp = rpol[settingname]
                    if settingname in upolicy:
                        up = upolicy[settingname]
                        if sp > up:
                            # policy bumps up user setting
                            upolicy[settingname] = sp
                        else:
                            # user policy is already better
                            pass
                    else:
                        # user policy doesn't have setting so force it
                        upolicy[settingname] = sp
                else:
                    # no setting of this name in the role's policy
                    pass
            else:
                # no policy for this role
                pass

    def policy_aggregate_policy_bool(self, upolicy, policy, roles, settingname):
        """
        Pull bool out of policy and install into upolicy if true
        param[in,out] upolicy user policy receiving aggregations
        param[in] policy Internal policy holding settings to be aggregated
        param[in] settingname setting of interest
        """
        if not 'policies' in policy:
            return
        policies = policy['policies']
        for role in roles:
            if role in policies:
                rpol = policies[role]
                if settingname in rpol:
                    if rpol[settingname]:
                        upolicy[settingname] = True
                else:
                    # no setting of this name in the role's policy
                    pass
            else:
                # no policy for this role
                pass

    def policy_aggregate_policy_list(self, upolicy, policy, roles, settingname):
        """
        Pull list out of policy and append into upolicy
        param[in,out] upolicy user policy receiving aggregations
        param[in] policy Internal policy holding settings to be aggregated
        param[in] settingname setting of interest
        """
        if not 'policies' in policy:
            return
        policies = policy['policies']
        for role in roles:
            if role in policies:
                rpol = policies[role]
                if settingname in rpol:
                    sp = rpol[settingname]
                    if settingname in upolicy:
                        upolicy[settingname].extend( sp )
                    else:
                        # user policy doesn't have setting so force it
                        upolicy[settingname] = sp
                else:
                    # no setting of this name in the role's policy
                    pass
            else:
                # no policy for this role
                pass

    def policy_lookup(self, user, host, app, upolicy):
        """
        Determine if a user on host accessing app is allowed.
        @param[in] user connection authId
        @param[in] host connection remote host numeric IP address
        @param[in] app application user is accessing
        @param[out] upolicy dict holding connection and policy values
        @return if allowed by policy
        # TODO: use lookaside list for precomputed (user, host, app) policy
        # Note: the upolicy output is a non-nested dict with settings of interest
        # TODO: figure out decent defaults for upolicy settings that are undefined
        """
        try:
            settings = self.data[app]
            # User allowed to connect from host?
            allowed = False
            restricted = False
            uhs = HostStruct(host)
            uroles = []
            if 'roles' in settings:
                for r in settings['roles']:
                    if user in settings['roles'][r]:
                        restricted = True
                        uroles.append(r)
                        #print "XXX user %s has roles %s " % (user, uroles)
            uorigins = []
            if 'connectionPolicy' in settings:
                for ur in uroles:
                    if ur in settings['connectionPolicy']:
                        uorigins.extend(settings['connectionPolicy'][ur])
                        #print "XXX user %s has origins %s" % (user, uorigins)
            if 'connectionOrigins' in settings:
                for co in settings['connectionOrigins']:
                    if co in uorigins:
                        for cohost in settings['connectionOrigins'][co]:
                            if cohost.match_bin(uhs):
                                #print "XXX user %s passes origin test at %s" % (user, uhs.dump())
                                allowed = True
                                break
                    if allowed:
                        break
            if not allowed and not restricted:
                if 'connectionAllowUnrestricted' in settings:
                    allowed = settings['connectionAllowUnrestricted']
            if not allowed:
                return False
            # Return connection limits and aggregation of role settings
            uroles.append(user) # user roles also includes username directly
            self.policy_aggregate_limits     (upolicy, settings, "policyVersion")
            self.policy_aggregate_limits     (upolicy, settings, "maximumConnections")
            self.policy_aggregate_limits     (upolicy, settings, "maximumConnectionsPerUser")
            self.policy_aggregate_limits     (upolicy, settings, "maximumConnectionsPerHost")
            self.policy_aggregate_policy_int (upolicy, settings, uroles, "max_frame_size")
            self.policy_aggregate_policy_int (upolicy, settings, uroles, "max_message_size")
            self.policy_aggregate_policy_int (upolicy, settings, uroles, "max_session_window")
            self.policy_aggregate_policy_int (upolicy, settings, uroles, "max_sessions")
            self.policy_aggregate_policy_int (upolicy, settings, uroles, "max_senders")
            self.policy_aggregate_policy_int (upolicy, settings, uroles, "max_receivers")
            self.policy_aggregate_policy_bool(upolicy, settings, uroles, "allow_dynamic_src")
            self.policy_aggregate_policy_bool(upolicy, settings, uroles, "allow_anonymous_sender")
            self.policy_aggregate_policy_list(upolicy, settings, uroles, "sources")
            self.policy_aggregate_policy_list(upolicy, settings, uroles, "targets")
            return True
        except Exception, e:
            #print str(e)
            #pdb.set_trace()
            return False


#
# HACK ALERT: Temporary
# Functions related to main
#
class ExitStatus(Exception):
    """Raised if a command wants a non-0 exit status from the script"""
    def __init__(self, status): self.status = status

def main_except(argv):

    usage = "usage: %prog [options]\nRead and print all conf files in a folder."
    parser = optparse.OptionParser(usage=usage)
    parser.set_defaults(folder="../../../tests/policy-1")
    parser.add_option("-f", "--folder", action="store", type="string", dest="folder",
                      help="Use named folder instead of policy-1")
    parser.add_option("-d", "--dump", action="store_true", dest="dump",
                      help="Dump policy details")

    (options, args) = parser.parse_args()

    policy = Policy(options.folder)

    print("policy names: %s" % policy.policy_db_get_names())

    if options.dump:
        print("Policy details:")
        for pname in policy.policy_db_get_names():
            print("policy : %s" % pname)
            p = ("%s" % policy.policy_read(pname))
            print(p.replace('\\n', '\n'))

    # Lookups
    upolicy = {}
    res = policy.policy_lookup('zeke', '192.168.100.5', 'photoserver', upolicy)
    print "Lookup zeke from 192.168.100.5. Expect true and max_frame_size 44444. Result is %s" % res
    print "Resulting policy is: %s" % upolicy

    upolicy = {}
    res = policy.policy_lookup('ellen', '72.135.2.9', 'photoserver', upolicy)
    print "Lookup ellen from 72.135.2.9. Expect true and max_frame_size 666666. Result is %s" % res
    print "Resulting policy is: %s" % upolicy


def main(argv):
    try:
        main_except(argv)
        return 0
    except ExitStatus, e:
        return e.status
    except Exception, e:
        print "%s: %s"%(type(e).__name__, e)
        return 1

if __name__ == "__main__":
    sys.exit(main(sys.argv))
