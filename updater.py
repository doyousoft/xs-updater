#!/usr/bin/env python3
#
# xs-updater: utility to fetch and apply updates to a XenServer pool
#
# Copyright (c) 2016- Doyousoft SA
#
# This file is part of xs-updater.
#
# xs-updater is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# xs-updater is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with xs-updater.  If not, see <http://www.gnu.org/licenses/>.

import http.client
import sys, time
import requests
import tempfile
import XenAPI
import os

from tqdm import tqdm
from zipfile import ZipFile
from pathlib import Path
from pprint import pprint
from lxml import etree

# FIXME:
storedUpdatesPath = os.environ['HOME']+"/Downloads/"

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print ("Usage:")
        print (sys.argv[0], " <url> <username> <password>")
        sys.exit(1)

    url = sys.argv[1]
    username = sys.argv[2]
    password = sys.argv[3]

    try:
        # First acquire a valid session by logging in:
        session = XenAPI.Session(url)
        session.xenapi.login_with_password(username, password)
    except XenAPI.Failure as e:
        if e.details[0]=='HOST_IS_SLAVE':
            url = 'https://'+e.details[1]
            session=XenAPI.Session(url)
            session.login_with_password(username, password)
        else:
            raise

    # Management details:
    xapiSession = session.xenapi.session.get_record( session._session )
    currentHost = session.xenapi.host.get_record( xapiSession['this_host'] )
    print( "Connected on ",currentHost['hostname']," running ", currentHost['software_version']['product_brand'], currentHost['software_version']['product_version'] )

    # Fetch all patch on the pool
    patches = session.xenapi.pool_patch.get_all_records ()
    appliedPatches = set ()

    for patch in patches:
        appliedPatches.add(patches[patch]['uuid'])

    print( "Fetching XenServer Update repository" )
    xsUpdates = requests.get( "http://updates.xensource.com/XenServer/updates.xml" )

    # Use a progress download ? need a byte=>str conversion
    # xsUpdates = requests.get( "http://updates.xensource.com/XenServer/updates.xml", stream=True )
    # xmlData = ""
    # for data in tqdm( xsUpdates.iter_content (), unit='B', total=int( xsUpdates.headers['Content-Length'] ), unit_scale=True):
    #     xmlData = xmlData + data
    #

    if xsUpdates.status_code == 200:
        xsuTree = etree.XML( xsUpdates.text )

    patches = dict ()
    pathStr = "/patchdata/serverversions/version[@build-number='"+currentHost['software_version']['build_number']+"']"

    # Extraction des nouveaux patches depuis le repository:
    for version in xsuTree.xpath( pathStr ):

        print( "Version %s" % version.get("name") )

        for patch in version.findall( "patch" ):

            detPatch = xsuTree.xpath( "/patchdata/patches/patch[@uuid='"+patch.get("uuid")+"']" )

            if( len( detPatch ) != 1 ):
                print( "Error, more than one matching patch !")
                sys.exit( -1 )
            else:
                if( patch.get( "uuid" ) not in appliedPatches):

                    patches[ detPatch[0].get( "name-label" ) ] = {
                        'name': detPatch[0].get( "name-label" ),
                        'description': detPatch[0].get( "name-description" ),
                        'url': detPatch[0].get( 'patch-url' ),
                        'uuid' : detPatch[0].get( 'uuid' )
                    }

    with tempfile.TemporaryDirectory() as dirpath:

        # FIXME: use threading to optimize download/upload process

        for patch in sorted(patches.keys()):
            cp = patches[ patch ]
            patchName = cp[ 'name' ]

            print( "Patch: %s" % cp[ "uuid" ] )
            print( "  %s ( %s ) " % (cp[ "name" ], cp[ "description" ] ) )

            if Path(storedUpdatesPath+"/"+patchName+".xsupdate").is_file() == False:
                # Patch not available in local cache, downloading

                reqPatch = requests.get (patches[patch]['url'], stream=True)

                print ("  Downloading "+patches[patch]['url']+': %.3f' % (int(reqPatch.headers['Content-Length'])/1024/1024)+"Mo")

                with open(dirpath+'/'+patchName+'.zip', 'wb') as f:
                    for data in tqdm(reqPatch.iter_content(), unit='B', total=int(reqPatch.headers['Content-Length']), unit_scale=True):
                        f.write(data)

                print ("  Extracting main content")
                with ZipFile(dirpath+'/'+patchName+'.zip', 'r') as myzip:
                    with myzip.open (patchName+".xsupdate") as rf:
                        print ("   saving into "+storedUpdatesPath+"/"+patchName+".xsupdate")
                        with open(storedUpdatesPath+"/"+patchName+".xsupdate","wb") as wf:
                            wf.write ( rf.read () )

            # Read file and pipe thru XS API
            print ("  Uploading patch to XenAPI")
            task = session.xenapi.task.create("import "+patchName+".xsupdate", "")

            with open(storedUpdatesPath+"/"+patchName+".xsupdate","rb") as rf:
                put_url = "%s/pool_patch_upload?session_id=%s&task_id=%s" % (url, session._session, task)
                print ("Upload to %s" % put_url )

                # Ugly Hack to downgrade to HTTP/1.0
                #  XenAPI doesn't react correctly to PUT in HTTP/1.1:
                #  no http reponse is sent back, the task is not updated
                #  but still the patch is added localy
                http.client.HTTPConnection._http_vsn = 10
                http.client.HTTPConnection._http_vsn_str = 'HTTP/1.0'

                response = requests.put(put_url, data=rf, headers={'Connection': None} )

                http.client.HTTPConnection._http_vsn = 11
                http.client.HTTPConnection._http_vsn_str = 'HTTP/1.1'

                finished = False
                while not finished:
                    finished = (session.xenapi.task.get_status (task) == "success")
                    print ("   Patch upload processing status: %s" % session.xenapi.task.get_status (task) )
                    time.sleep (1)

                result = session.xenapi.task.get_result (task)

                print ("  Applying patch:")

                session.xenapi.pool_patch.pool_apply (result)
                print ("   Post install: %s" % session.xenapi.pool_patch.get_after_apply_guidance (result) )
                #time.sleep(400)

    print ("Done")

    print ("Post upgrade step: reboot hosts")

    print ("Reboot management node")

    print ("Evacuate host")
    session.xenapi.host.evacuate (xapiSession['this_host'])

    print ("VMs running on %s:" % session.xenapi.host.get_name_label(xapiSession['this_host']) )

    for vm in session.xenapi.host.get_resident_VMs (xapiSession['this_host']):
        print (" %s" % session.xenapi.VM.get_name_label (vm) )


    hosts = session.xenapi.host.get_all()

    for x in hosts:
        if (x == xapiSession['this_host']):
            print ("Management node")
            next

        print ("VMs running on %s:" % session.xenapi.host.get_name_label(x) )

        for vm in session.xenapi.host.get_resident_VMs (x):
            print (" %s" % session.xenapi.VM.get_name_label (vm) )

    session.xenapi.session.logout()
