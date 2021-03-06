# Copyright 2014 Cloudera Inc.
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


# flake8: noqa

__version__ = '0.3.0'

from ibis.client import ImpalaConnection, ImpalaClient
from ibis.filesystems import HDFS, WebHDFS
from ibis.common import IbisError

import ibis.expr.api as api
import ibis.expr.types as ir
import ibis.sql.udf as udf

# __all__ is defined
from ibis.expr.api import *

import ibis.config_init
from ibis.config import options


def make_client(db, hdfs_client=None):
    """
    Create an Ibis client from a database connection and optional additional
    connections (like HDFS)

    Parameters
    ----------
    db : Connection
      e.g. produced by ibis.impala_connect
    hdfs_client : ibis HDFS client

    Examples
    --------
    >>> con = ibis.impala_connect(**impala_params)
    >>> hdfs = ibis.hdfs_connect(**hdfs_params)
    >>> client = ibis.make_client(con, hdfs_client=hdfs)

    Returns
    -------
    client : IbisClient
    """
    return ImpalaClient(db, hdfs_client=hdfs_client)


def impala_connect(host='localhost', port=21050, protocol='hiveserver2',
                   database='default', timeout=45, use_ssl=False, ca_cert=None,
                   use_ldap=False, ldap_user=None, ldap_password=None,
                   use_kerberos=False, kerberos_service_name='impala',
                   pool_size=8):
    """
    Create an Impala Client for use with Ibis

    Parameters
    ----------
    host : host name
    port : int, default 21050 (HiveServer 2)
    protocol : {'hiveserver2', 'beeswax'}
    database :
    timeout :
    use_ssl :
    ca_cert :
    use_ldap : boolean, default False
    ldap_user :
    ldap_password :
    use_kerberos : boolean, default False
    kerberos_service_name : string, default 'impala'

    Returns
    -------
    con : ImpalaConnection
    """
    params = {
        'host': host,
        'port': port,
        'protocol': protocol,
        'database': database,
        'timeout': timeout,
        'use_ssl': use_ssl,
        'ca_cert': ca_cert,
        'use_ldap': use_ldap,
        'ldap_user': ldap_user,
        'ldap_password': ldap_password,
        'use_kerberos': use_kerberos,
        'kerberos_service_name': kerberos_service_name
    }

    return ImpalaConnection(pool_size=pool_size, **params)


def hdfs_connect(host='localhost', port=50070, protocol='webhdfs',
                 use_kerberos=False, verify=True, **kwds):
    """
    Connect to HDFS

    Parameters
    ----------
    host : string
    port : int, default 50070 (webhdfs default)
    protocol : {'webhdfs'}
    use_kerberos : boolean, default False
    verify : boolean, default False
        Set to False to turn off verifying SSL certificates

    Other keywords are forwarded to hdfs library classes

    Returns
    -------
    client : ibis HDFS client
    """
    if use_kerberos:
        try:
            import requests_kerberos
        except ImportError:
            raise IbisError(
                "Unable to import requests-kerberos, which is required for "
                "Kerberos HDFS support. Install it by executing `pip install "
                "requests-kerberos` or `pip install hdfs[kerberos]`.")
        from hdfs.ext.kerberos import KerberosClient
        url = 'https://{0}:{1}'.format(host, port) # note SSL
        hdfs_client = KerberosClient(url, mutual_auth='OPTIONAL',
                                     verify=verify, **kwds)
    else:
        from hdfs.client import InsecureClient
        url = 'http://{0}:{1}'.format(host, port)
        hdfs_client = InsecureClient(url, verify=verify, **kwds)
    return WebHDFS(hdfs_client)


def wrap_impala_uda(hfds_file, inputs, output, init_fn, update_fn,
                    merge_fn, finalize_fn, name=None):
    """
    Creates and returns a useful container object that can be used to
    issue a create_uda() statement and register the uda within ibis

    Parameters
    ----------
    hdfs_file: .so file that contains relevant UDA
    inputs: list of strings denoting ibis datatypes
    output: string denoting ibis datatype
    init_fn: string, C++ function name for initialization function
    update_fn: string, C++ function name for update function
    merge_fn: string, C++ function name for merge function
    finalize_fn: C++ function name for finalize function
    name: string (optional). Used internally to track function

    Returns
    -------
    container : UDA object
    """
    return udf.UDACreator(hdfs_file, inputs, output, init_fn,
                          update_fn, merge_fn, finalize_fn,
                          name=name)
    

def wrap_impala_udf(hdfs_file, inputs, output, so_symbol, name=None):
    """
    Creates and returns a useful container object that can be used to
    issue a create_udf() statement and register the udf within ibis

    Parameters
    ----------
    hdfs_file: .so file that contains relevant UDF
    inputs: list of strings denoting ibis datatypes
    output: string denoting ibis datatype
    so_symbol: string, C++ function name for relevant UDF
    name: string (optional). Used internally to track function

    Returns
    -------
    container : UDF object
    """
    return udf.UDFCreator(hdfs_file, inputs, output, so_symbol, name=name)


def scalar_function(inputs, output, name=None):
    """
    Creates and returns an operator class that can be
    passed to add_impala_operation()

    Parameters:
    inputs: list of strings denoting ibis datatypes
    output: string denoting ibis datatype
    name: string (optional). Used internally to track function

    Returns
    -------
    op : operator class to use in construction function
    """
    return udf.scalar_function(inputs, output, name=name)


def add_impala_operation(op, name, database):
    """
    Registers the given operation within the Ibis
    framework so it can be used

    Parameters
    ----------
    op: operator class
    name: used in issuing statements to SQL engine
    database: database the relevant operator is registered to
    """
    udf.add_impala_operation(op, name, database)
    

def test(include_e2e=False):
    import pytest
    import ibis
    import os

    ibis_dir, _ = os.path.split(ibis.__file__)

    args = ['--pyargs', ibis_dir]
    if include_e2e:
        args.append('--e2e')
    pytest.main(args)
