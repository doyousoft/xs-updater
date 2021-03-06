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

from termcolor import colored

from tqdm import tqdm
from zipfile import ZipFile
from pathlib import Path
from pprint import pprint
from lxml import etree

from pprint import pprint

# FIXME:
storedUpdatesPath = os.environ['HOME']+"/Downloads/"

if __name__ == "__main__":
    if len( sys.argv ) != 4:
        print( "Usage:" )
        print( sys.argv[0], " <url> <username> <password>" )
        sys.exit( 1 )

    url = sys.argv[1]
    username = sys.argv[2]
    password = sys.argv[3]

    try:
        # First acquire a valid session by logging in:
        session = XenAPI.Session( url )
        session.xenapi.login_with_password( username, password )
    except XenAPI.Failure as e:
        if e.details[0]=='HOST_IS_SLAVE':
            print( colored( "**Reconnecting to MASTER host: %s" % e.details[1], 'yellow' ) )
            url = 'https://'+e.details[1]
            session=XenAPI.Session( url )
            session.login_with_password( username, password )
        else:
            raise

    # Management details:
    xapiSession = session.xenapi.session.get_record( session._session )
    currentHost = session.xenapi.host.get_record( xapiSession['this_host'] )
    
    print( colored ("Connected on %s running %s (%s)" % ( currentHost['hostname'], currentHost['software_version']['product_brand'], currentHost['software_version']['product_version'] ) , 'blue' ) )
    
    lVer = currentHost['software_version']['product_version'] . split (".")

    patchMode = True
    patchExt = ".xsupdate"
    pathStr = "/patchdata/serverversions/version[@build-number='"+currentHost['software_version']['build_number']+"']"
    if int(lVer[0]) >= 7 and int(lVer[1]) >= 1:
        # At version 7.1 there seem to be a few changes in patch system

        # Search within XML by version string instead of build-number who changed format in 7.2 and isn't populated in the XML
        pathStr = "/patchdata/serverversions/version[@value='"+currentHost['software_version']['product_version']+"']"

        # The latest XenServer use a new "update" mechanism with iso images files and new commands 
        patchExt = ".iso"
        patchMode = False

    # Fetch all patch on the pool
    # FIXME: handle patch unapplied on some host ?
    if patchMode:
        patches = session.xenapi.pool_patch.get_all_records ()
    else:
        patches = session.xenapi.pool_update.get_all_records ()
    appliedPatches = set ()

    for patch in patches:
        appliedPatches.add(patches[patch]['uuid'])

    print( colored( "Fetching XenServer Update repository", 'green') )
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

    # Extraction des nouveaux patches depuis le repository:
    for version in xsuTree.xpath( pathStr ):

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
                    print( colored( " Patch %s (%s) missing on the pool" %( patches[ detPatch[0].get( "name-label") ]['name'], patches[ detPatch[0].get( "name-label") ]['description'] ), 'white'))

    with tempfile.TemporaryDirectory() as dirpath:

        # FIXME: use threading to optimize download/upload process
        for patch in sorted(patches.keys()):
            cp = patches[ patch ]
            patchName = cp[ 'name' ]

            print( colored( "Patch: %s" % cp[ "uuid" ], 'green' ) )
            print( colored( "  %s ( %s ) " % (cp[ "name" ], cp[ "description" ]), "blue" ) )

            if Path( storedUpdatesPath+"/"+patchName+patchExt ).is_file() == False:
                # Patch not available in local cache, downloading

                reqPatch = requests.get( patches[patch]['url'], stream=True )

                print( colored( "  Downloading "+patches[patch]['url']+': %.3f' % (int(reqPatch.headers['Content-Length'])/1024/1024)+"Mo", "green" ) )

                with open ( dirpath+'/'+patchName+'.zip', 'wb' ) as f:
                    pbar = tqdm ( unit='B', total=int( reqPatch.headers['Content-Length'] ), maxinterval=10*1024, unit_scale=True)
                    for data in reqPatch . iter_content( 1024 ):
                        pbar . update ( len (data) )
                        f . write( data )

                print ( colored( "  Extracting main content", "green" ) )
                with ZipFile(dirpath+'/'+patchName+'.zip', 'r') as myzip:
                    with myzip.open (patchName+patchExt) as rf:
                        print( "   saving into "+storedUpdatesPath+"/"+patchName+patchExt)
                        with open( storedUpdatesPath+"/"+patchName+patchExt,"wb" ) as wf:
                            wf.write( rf.read() )

            # Read file and pipe thru XS API
            # FIXME: some disk checks would be quite usefull
            print( colored( "  Uploading patch to XenAPI", "green" ) )
            task = session.xenapi.task.create( "import "+patchName+patchExt, "" )

            with open(storedUpdatesPath+"/"+patchName+patchExt,"rb") as rf:
                put_url = "%s/pool_patch_upload?session_id=%s&task_id=%s" % (url, session._session, task)
                print( colored( "Upload to %s" % put_url, "yellow" ) )

                # Ugly Hack to downgrade to HTTP/1.0
                #  XenAPI doesn't react correctly to PUT in HTTP/1.1:
                #  no http reponse is sent back, the task is not updated
                #  but still the patch is added localy
                http.client.HTTPConnection._http_vsn = 10
                http.client.HTTPConnection._http_vsn_str = 'HTTP/1.0'

                # FIXME: an upload progress bar would be quite usefull ...
                response = requests.put(put_url, data=rf, headers={'Connection': None} )

                http.client.HTTPConnection._http_vsn = 11
                http.client.HTTPConnection._http_vsn_str = 'HTTP/1.1'

                finished = False
                while not finished:
                    finished = (session.xenapi.task.get_status (task) == "success")
                    print( "   Patch upload processing status: %s" % session.xenapi.task.get_status( task ) )
                    time.sleep (1)

                result = session.xenapi.task.get_result( task )

                print ( colored( "  Applying patch:", "green" ) )

                if patchMode:
                    session.xenapi.pool_patch.pool_apply( result )
                else:
                    session.xenapi.pool_update.pool_apply( result )

                print( colored( "   Post install: %s" % session.xenapi.pool_patch.get_after_apply_guidance( result ), "blue" ) )
                #time.sleep(400)

    # FIXME: Disabled for now, need testing
    if ( 0 == 1 ) :
        
        print ( colored ( "Post upgrade step: reboot hosts", "blue" ) )

        print ( colored ( "Reboot management node", "green" ) )

        print ( colored ( "Evacuate host ?", "green" ) )
        #session.xenapi.host.evacuate( xapiSession['this_host'] )

        print ( colored ( "VMs running on %s:" % session.xenapi.host.get_name_label( xapiSession['this_host'] ), "blue" ) )

        for vm in session.xenapi.host.get_resident_VMs( xapiSession['this_host'] ):
            print( colored( " %s" % session.xenapi.VM.get_name_label( vm ), "blue" ) )


        hosts = session.xenapi.host.get_all()

        for x in hosts:
            if ( x == xapiSession['this_host'] ):
                print( "Management node, skipping" )
                next

            print( "VMs running on %s:" % session.xenapi.host.get_name_label( x ) )

            for vm in session.xenapi.host.get_resident_VMs( x ):
                print (" %s" % session.xenapi.VM.get_name_label( vm ) )

    session.xenapi.session.logout()
