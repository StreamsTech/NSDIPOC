# -*- coding: utf-8 -*-
#########################################################################
#
# Copyright (C) 2016 OSGeo
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################

"""Utilities for managing GeoNode layers
"""

# Standard Modules
import logging
import re
import os
import glob
import sys
import tempfile
import json
import string
import random
import subprocess
import requests


from osgeo import gdal, osr

# Django functionality
from django.contrib.auth import get_user_model
from django.template.defaultfilters import slugify
from django.core.exceptions import ObjectDoesNotExist
from django.core.files.storage import default_storage as storage
from django.core.files import File
from django.conf import settings
from django.db.models import Q
from django.db import connection
from django.utils.translation import ugettext as _

# Geonode functionality
from geonode import GeoNodeException
from geonode.people.utils import get_valid_user
from geonode.layers.models import Layer, UploadSession
from geonode.base.models import Link, SpatialRepresentationType, TopicCategory, Region
from geonode.layers.models import shp_exts, csv_exts, vec_exts, cov_exts
from geonode.layers.metadata import set_metadata
from geonode.utils import http_client

import tarfile

from zipfile import ZipFile, is_zipfile

from datetime import datetime

logger = logging.getLogger('geonode.layers.utils')

_separator = '\n' + ('-' * 100) + '\n'


def _clean_string(
        str,
        regex=r"(^[^a-zA-Z\._]+)|([^a-zA-Z\._0-9]+)",
        replace="_"):
    """
    Replaces a string that matches the regex with the replacement.
    """
    regex = re.compile(regex)

    if str[0].isdigit():
        str = replace + str

    return regex.sub(replace, str)


def resolve_regions(regions):

    regions_resolved = []
    regions_unresolved = []
    if regions:
        if len(regions) > 0:
            for region in regions:
                try:
                    region_resolved = Region.objects.get(Q(name__iexact=region) | Q(code__iexact=region))
                    regions_resolved.append(region_resolved)
                except ObjectDoesNotExist:
                    regions_unresolved.append(region)

    return regions_resolved, regions_unresolved


def get_files(filename):
    """Converts the data to Shapefiles or Geotiffs and returns
       a dictionary with all the required files
    """
    files = {}

    # Verify if the filename is in ascii format.
    try:
        filename.decode('ascii')
    except UnicodeEncodeError:
        msg = "Please use only characters from the english alphabet for the filename. '%s' is not yet supported." \
            % os.path.basename(filename).encode('UTF-8')
        raise GeoNodeException(msg)

    # Make sure the file exists.
    if not os.path.exists(filename):
        msg = ('Could not open %s. Make sure you are using a '
               'valid file' % filename)
        logger.warn(msg)
        raise GeoNodeException(msg)

    base_name, extension = os.path.splitext(filename)
    # Replace special characters in filenames - []{}()
    glob_name = re.sub(r'([\[\]\(\)\{\}])', r'[\g<1>]', base_name)

    if extension.lower() == '.shp':
        required_extensions = dict(
            shp='.[sS][hH][pP]', dbf='.[dD][bB][fF]', shx='.[sS][hH][xX]')
        for ext, pattern in required_extensions.iteritems():
            matches = glob.glob(glob_name + pattern)
            if len(matches) == 0:
                msg = ('Expected helper file %s does not exist; a Shapefile '
                       'requires helper files with the following extensions: '
                       '%s') % (base_name + "." + ext,
                                required_extensions.keys())
                raise GeoNodeException(msg)
            elif len(matches) > 1:
                msg = ('Multiple helper files for %s exist; they need to be '
                       'distinct by spelling and not just case.') % filename
                raise GeoNodeException(msg)
            else:
                files[ext] = matches[0]

        matches = glob.glob(glob_name + ".[pP][rR][jJ]")
        if len(matches) == 1:
            files['prj'] = matches[0]
        elif len(matches) > 1:
            msg = ('Multiple helper files for %s exist; they need to be '
                   'distinct by spelling and not just case.') % filename
            raise GeoNodeException(msg)

    elif extension.lower() in cov_exts:
        files[extension.lower().replace('.', '')] = filename

    if 'geonode.geoserver' in settings.INSTALLED_APPS:
        matches = glob.glob(glob_name + ".[sS][lL][dD]")
        if len(matches) == 1:
            files['sld'] = matches[0]
        elif len(matches) > 1:
            msg = ('Multiple style files (sld) for %s exist; they need to be '
                   'distinct by spelling and not just case.') % filename
            raise GeoNodeException(msg)

    matches = glob.glob(base_name + ".[xX][mM][lL]")

    # shapefile XML metadata is sometimes named base_name.shp.xml
    # try looking for filename.xml if base_name.xml does not exist
    if len(matches) == 0:
        matches = glob.glob(filename + ".[xX][mM][lL]")

    if len(matches) == 1:
        files['xml'] = matches[0]
    elif len(matches) > 1:
        msg = ('Multiple XML files for %s exist; they need to be '
               'distinct by spelling and not just case.') % filename
        raise GeoNodeException(msg)

    if 'geonode_qgis_server' in settings.INSTALLED_APPS:
        matches = glob.glob(glob_name + ".[qQ][mM][lL]")
        logger.debug('Checking QML file')
        logger.debug('Number of matches QML file : %s' % len(matches))
        logger.debug('glob name: %s' % glob_name)
        if len(matches) == 1:
            files['qml'] = matches[0]
        elif len(matches) > 1:
            msg = ('Multiple style files (qml) for %s exist; they need to be '
                   'distinct by spelling and not just case.') % filename
            raise GeoNodeException(msg)

    return files


def layer_type(filename):
    """Finds out if a filename is a Feature or a Vector
       returns a gsconfig resource_type string
       that can be either 'featureType' or 'coverage'
    """
    base_name, extension = os.path.splitext(filename)

    if extension.lower() == '.zip':
        zf = ZipFile(filename)
        # ZipFile doesn't support with statement in 2.6, so don't do it
        try:
            for n in zf.namelist():
                b, e = os.path.splitext(n.lower())
                if e in shp_exts or e in cov_exts or e in csv_exts:
                    extension = e
        finally:
            zf.close()

    if extension.lower() == '.tar' or filename.endswith('.tar.gz'):
        tf = tarfile.open(filename)
        # TarFile doesn't support with statement in 2.6, so don't do it
        try:
            for n in tf.getnames():
                b, e = os.path.splitext(n.lower())
                if e in shp_exts or e in cov_exts or e in csv_exts:
                    extension = e
        finally:
            tf.close()

    if extension.lower() in vec_exts:
        return 'vector'
    elif extension.lower() in cov_exts:
        return 'raster'
    else:
        msg = ('Saving of extension [%s] is not implemented' % extension)
        raise GeoNodeException(msg)


def get_valid_name(layer_name):
    """
    Create a brand new name
    """

    name = _clean_string(layer_name)
    proposed_name = name
    count = 1
    while Layer.objects.filter(name=proposed_name).exists():
        proposed_name = "%s_%d" % (name, count)
        count = count + 1
        logger.info('Requested name already used; adjusting name '
                    '[%s] => [%s]', layer_name, proposed_name)
    else:
        logger.info("Using name as requested")

    return proposed_name


def get_valid_layer_name(layer, overwrite):
    """Checks if the layer is a string and fetches it from the database.
    """
    # The first thing we do is get the layer name string
    if isinstance(layer, Layer):
        layer_name = layer.name
    elif isinstance(layer, basestring):
        layer_name = layer
    else:
        msg = ('You must pass either a filename or a GeoNode layer object')
        raise GeoNodeException(msg)

    if overwrite:
        return layer_name
    else:
        return get_valid_name(layer_name)


def get_default_user():
    """Create a default user
    """
    superusers = get_user_model().objects.filter(
        is_superuser=True).order_by('id')
    if superusers.count() > 0:
        # Return the first created superuser
        return superusers[0]
    else:
        raise GeoNodeException('You must have an admin account configured '
                               'before importing data. '
                               'Try: django-admin.py createsuperuser')


def is_vector(filename):
    __, extension = os.path.splitext(filename)

    if extension in vec_exts:
        return True
    else:
        return False


def is_raster(filename):
    __, extension = os.path.splitext(filename)

    if extension in cov_exts:
        return True
    else:
        return False


def get_resolution(filename):
    gtif = gdal.Open(filename)
    gt = gtif.GetGeoTransform()
    __, resx, __, __, __, resy = gt
    resolution = '%s %s' % (resx, resy)
    return resolution


def get_bbox(filename):
    from django.contrib.gis.gdal import DataSource
    bbox_x0, bbox_y0, bbox_x1, bbox_y1 = None, None, None, None

    if is_vector(filename):
        datasource = DataSource(filename)
        layer = datasource[0]
        bbox_x0, bbox_y0, bbox_x1, bbox_y1 = layer.extent.tuple

    elif is_raster(filename):
        gtif = gdal.Open(filename)
        gt = gtif.GetGeoTransform()
        cols = gtif.RasterXSize
        rows = gtif.RasterYSize

        ext = []
        xarr = [0, cols]
        yarr = [0, rows]

        # Get the extent.
        for px in xarr:
            for py in yarr:
                x = gt[0] + (px * gt[1]) + (py * gt[2])
                y = gt[3] + (px * gt[4]) + (py * gt[5])
                ext.append([x, y])

            yarr.reverse()

        # ext has four corner points, get a bbox from them.
        bbox_x0 = ext[0][0]
        bbox_y0 = ext[0][1]
        bbox_x1 = ext[2][0]
        bbox_y1 = ext[2][1]

    return [bbox_x0, bbox_x1, bbox_y0, bbox_y1]


def unzip_file(upload_file, extension='.shp', tempdir=None):
    """
    Unzips a zipfile into a temporary directory and returns the full path of the .shp file inside (if any)
    """
    absolute_base_file = None
    if tempdir is None:
        tempdir = tempfile.mkdtemp()

    the_zip = ZipFile(upload_file)

    the_zip.extractall(tempdir)
    for item in the_zip.namelist():
        if item.endswith(extension):
            absolute_base_file = os.path.join(tempdir, item)

    return absolute_base_file



#@jahangir091
def ogrinfo(file_path):
    from plumbum.cmd import ogrinfo
    output_string = ogrinfo(file_path)
    point_layer = '(Point)'
    line_layer = '(Line String)'
    multi_line_layer = '(Multi Line String)'
    multipolygon_layer = '(Geometry Collection)'
    layers = []
    if point_layer in output_string:
        layers.append('points')
    if line_layer in output_string:
        layers.append('lines')
    if multi_line_layer in output_string:
        layers.append('multilinestrings')
    if multipolygon_layer in output_string:
        layers.append('multipolygons')
    return layers
#end


def extract_tarfile(upload_file, extension='.shp', tempdir=None):
    """
    Extracts a tarfile into a temporary directory and returns the full path of the .shp file inside (if any)
    """
    absolute_base_file = None
    if tempdir is None:
        tempdir = tempfile.mkdtemp()

    the_tar = tarfile.open(upload_file)
    the_tar.extractall(tempdir)
    for item in the_tar.getnames():
        if item.endswith(extension):
            absolute_base_file = os.path.join(tempdir, item)

    return absolute_base_file


def file_upload(filename, name=None, group=None, user=None, title=None, abstract=None,
                keywords=None, category=None, regions=None, date=None,
                skip=True, overwrite=False, charset='UTF-8',
                metadata_uploaded_preserve=False, status=None, user_data_epsg=None):
    """Saves a layer in GeoNode asking as little information as possible.
       Only filename is required, user and title are optional.
    """
    if keywords is None:
        keywords = []
    if regions is None:
        regions = []

    # Get a valid user
    theuser = get_valid_user(user)

    # Create a new upload session
    upload_session = UploadSession.objects.create(user=theuser)

    # Get all the files uploaded with the layer
    files = get_files(filename)

    # Set a default title that looks nice ...
    if title is None:
        basename = os.path.splitext(os.path.basename(filename))[0]
        title = basename.title().replace('_', ' ')

    # Create a name from the title if it is not passed.
    if name is None:
        name = slugify(title).replace('-', '_')

    if category is not None:
        categories = TopicCategory.objects.filter(Q(identifier__iexact=category) | Q(gn_description__iexact=category))
        if len(categories) == 1:
            category = categories[0]
        else:
            category = None

    # Generate a name that is not taken if overwrite is False.
    valid_name = get_valid_layer_name(name, overwrite)

    # Add them to the upload session (new file fields are created).
    assigned_name = None
    for type_name, fn in files.items():
        with open(fn, 'rb') as f:
            upload_session.layerfile_set.create(name=type_name,
                                                file=File(f, name='%s.%s' % (assigned_name or valid_name, type_name)))
            # save the system assigned name for the remaining files
            if not assigned_name:
                the_file = upload_session.layerfile_set.all()[0].file.name
                assigned_name = os.path.splitext(os.path.basename(the_file))[0]

    # Get a bounding box
    bbox_x0, bbox_x1, bbox_y0, bbox_y1 = get_bbox(filename)

    # by default, if RESOURCE_PUBLISHING=True then layer.is_published
    # must be set to False
    is_published = True
    if settings.RESOURCE_PUBLISHING:
        is_published = False
    if not status:
        status = 'DRAFT'

    defaults = {
        'upload_session': upload_session,
        'title': title,
        'abstract': abstract,
        'owner': user,
        'charset': charset,
        'bbox_x0': bbox_x0,
        'bbox_x1': bbox_x1,
        'bbox_y0': bbox_y0,
        'bbox_y1': bbox_y1,
        'is_published': is_published,
        'category': category,
        'group': group,
        'status': status,
        'user_data_epsg': user_data_epsg
    }

    # set metadata
    if 'xml' in files:
        with open(files['xml']) as f:
            xml_file = f.read()
        defaults['metadata_uploaded'] = True
        defaults['metadata_uploaded_preserve'] = metadata_uploaded_preserve

        # get model properties from XML
        identifier, vals, regions, keywords = set_metadata(xml_file)

        if defaults['metadata_uploaded_preserve']:
            defaults['metadata_xml'] = xml_file
            defaults['uuid'] = identifier

        for key, value in vals.items():
            if key == 'spatial_representation_type':
                value = SpatialRepresentationType(identifier=value)
            elif key == 'topic_category':
                value, created = TopicCategory.objects.get_or_create(
                    identifier=value.lower(),
                    defaults={'description': '', 'gn_description': value})
                key = 'category'
                defaults[key] = value
            else:
                defaults[key] = value

    regions_resolved, regions_unresolved = resolve_regions(regions)
    keywords.extend(regions_unresolved)

    if getattr(settings, 'NLP_ENABLED', False):
        try:
            from geonode.contrib.nlp.utils import nlp_extract_metadata_dict
            nlp_metadata = nlp_extract_metadata_dict({
                'title': defaults.get('title', None),
                'abstract': defaults.get('abstract', None),
                'purpose': defaults.get('purpose', None)})
            if nlp_metadata:
                regions_resolved.extend(nlp_metadata.get('regions', []))
                keywords.extend(nlp_metadata.get('keywords', []))
        except:
            print "NLP extraction failed."

    # If it is a vector file, create the layer in postgis.
    if is_vector(filename):
        defaults['storeType'] = 'dataStore'

    # If it is a raster file, get the resolution.
    if is_raster(filename):
        defaults['storeType'] = 'coverageStore'

    # Create a Django object.
    layer, created = Layer.objects.get_or_create(
        name=valid_name,
        defaults=defaults
    )

    # Delete the old layers if overwrite is true
    # and the layer was not just created
    # process the layer again after that by
    # doing a layer.save()
    if not created and overwrite:
        if layer.upload_session:
            layer.upload_session.layerfile_set.all().delete()
        layer.upload_session = upload_session
        # Pass the parameter overwrite to tell whether the
        # geoserver_post_save_signal should upload the new file or not
        layer.overwrite = overwrite
        layer.save()

    # Assign the keywords (needs to be done after saving)
    keywords = list(set(keywords))
    if keywords:
        if len(keywords) > 0:
            layer.keywords.add(*keywords)

    # Assign the regions (needs to be done after saving)
    regions_resolved = list(set(regions_resolved))
    if regions_resolved:
        if len(regions_resolved) > 0:
            layer.regions.add(*regions_resolved)

    if date is not None:
        layer.date = datetime.strptime(date, '%Y-%m-%d %H:%M:%S')
        layer.save()

    return layer


def upload(incoming, user=None, overwrite=False,
           keywords=[], category=None, regions=[],
           skip=True, ignore_errors=True,
           verbosity=1, console=None, title=None, date=None,
           private=False, metadata_uploaded_preserve=False):
    """Upload a directory of spatial data files to GeoNode

       This function also verifies that each layer is in GeoServer.

       Supported extensions are: .shp, .tif, .tar, .tar.gz, and .zip (of a shapefile).
       It catches GeoNodeExceptions and gives a report per file
    """
    if verbosity > 1:
        print >> console, "Verifying that GeoNode is running ..."

    if console is None:
        console = open(os.devnull, 'w')

    potential_files = []
    if os.path.isfile(incoming):
        ___, short_filename = os.path.split(incoming)
        basename, extension = os.path.splitext(short_filename)
        filename = incoming

        if extension in ['.tif', '.shp', '.tar', '.zip']:
            potential_files.append((basename, filename))
        elif short_filename.endswith('.tar.gz'):
            potential_files.append((basename, filename))

    elif not os.path.isdir(incoming):
        msg = ('Please pass a filename or a directory name as the "incoming" '
               'parameter, instead of %s: %s' % (incoming, type(incoming)))
        logger.exception(msg)
        raise GeoNodeException(msg)
    else:
        datadir = incoming
        for root, dirs, files in os.walk(datadir):
            for short_filename in files:
                basename, extension = os.path.splitext(short_filename)
                filename = os.path.join(root, short_filename)
                if extension in ['.tif', '.shp', '.tar', '.zip']:
                    potential_files.append((basename, filename))
                elif short_filename.endswith('.tar.gz'):
                    potential_files.append((basename, filename))

    # After gathering the list of potential files,
    # let's process them one by one.
    number = len(potential_files)
    if verbosity > 1:
        msg = "Found %d potential layers." % number
        print >> console, msg

    output = []
    for i, file_pair in enumerate(potential_files):
        basename, filename = file_pair

        existing_layers = Layer.objects.filter(name=basename)

        if existing_layers.count() > 0:
            existed = True
        else:
            existed = False

        if existed and skip:
            save_it = False
            status = 'skipped'
            layer = existing_layers[0]
            if verbosity > 0:
                msg = ('Stopping process because '
                       '--overwrite was not set '
                       'and a layer with this name already exists.')
                print >> sys.stderr, msg
        else:
            save_it = True

        if save_it:
            try:
                if is_zipfile(filename):
                    filename = unzip_file(filename)

                if tarfile.is_tarfile(filename):
                    filename = extract_tarfile(filename)

                layer = file_upload(filename,
                                    user=user,
                                    overwrite=overwrite,
                                    keywords=keywords,
                                    category=category,
                                    regions=regions,
                                    title=title,
                                    date=date,
                                    metadata_uploaded_preserve=metadata_uploaded_preserve
                                    )
                if not existed:
                    status = 'created'
                else:
                    status = 'updated'
                if private and user:
                    perm_spec = {"users": {"AnonymousUser": [],
                                           user.username: ["change_resourcebase_metadata", "change_layer_data",
                                                           "change_layer_style", "change_resourcebase",
                                                           "delete_resourcebase", "change_resourcebase_permissions",
                                                           "publish_resourcebase"]}, "groups": {}}
                    layer.set_permissions(perm_spec)

                if getattr(settings, 'SLACK_ENABLED', False):
                    try:
                        from geonode.contrib.slack.utils import build_slack_message_layer, send_slack_messages
                        send_slack_messages(build_slack_message_layer(
                            ("layer_new" if status == "created" else "layer_edit"),
                            layer))
                    except:
                        print "Could not send slack message."

            except Exception as e:
                if ignore_errors:
                    status = 'failed'
                    exception_type, error, traceback = sys.exc_info()
                else:
                    if verbosity > 0:
                        msg = ('Stopping process because '
                               '--ignore-errors was not set '
                               'and an error was found.')
                        print >> sys.stderr, msg
                        msg = 'Failed to process %s' % filename
                        raise Exception(msg, e), None, sys.exc_info()[2]

        msg = "[%s] Layer for '%s' (%d/%d)" % (status, filename, i + 1, number)
        info = {'file': filename, 'status': status}
        if status == 'failed':
            info['traceback'] = traceback
            info['exception_type'] = exception_type
            info['error'] = error
        else:
            info['name'] = layer.name

        output.append(info)
        if verbosity > 0:
            print >> console, msg
    return output


def create_thumbnail(instance, thumbnail_remote_url, thumbnail_create_url=None,
                     check_bbox=True, ogc_client=None, overwrite=False):
    thumbnail_dir = os.path.join(settings.MEDIA_ROOT, 'thumbs')
    thumbnail_name = 'layer-%s-thumb.png' % instance.uuid
    thumbnail_path = os.path.join(thumbnail_dir, thumbnail_name)

    if overwrite is True or storage.exists(thumbnail_path) is False:
        if not ogc_client:
            ogc_client = http_client
        BBOX_DIFFERENCE_THRESHOLD = 1e-5

        if not thumbnail_create_url:
            thumbnail_create_url = thumbnail_remote_url

        if check_bbox:
            # Check if the bbox is invalid
            valid_x = (
                float(
                    instance.bbox_x0) -
                float(
                    instance.bbox_x1)) ** 2 > BBOX_DIFFERENCE_THRESHOLD
            valid_y = (
                float(
                    instance.bbox_y1) -
                float(
                    instance.bbox_y0)) ** 2 > BBOX_DIFFERENCE_THRESHOLD
        else:
            valid_x = True
            valid_y = True

        image = None

        if valid_x and valid_y:
            Link.objects.get_or_create(resource=instance.get_self_resource(),
                                       url=thumbnail_remote_url,
                                       defaults=dict(
                                           extension='png',
                                           name="Remote Thumbnail",
                                           mime='image/png',
                                           link_type='image',
                                           )
                                       )
            Layer.objects.filter(id=instance.id) \
                .update(thumbnail_url=thumbnail_remote_url)
            # Download thumbnail and save it locally.
            resp, image = ogc_client.request(thumbnail_create_url)
            if 'ServiceException' in image or \
               resp.status < 200 or resp.status > 299:
                msg = 'Unable to obtain thumbnail: %s' % image
                logger.debug(msg)
                # Replace error message with None.
                image = None

        if image is not None:
            filename = 'layer-%s-thumb.png' % instance.uuid
            instance.save_thumbnail(filename, image=image)


def reprojection(tmp_dir, base_file):
    command = 'ogr2ogr -f "ESRI Shapefile" -t_srs EPSG:4326 ' + tmp_dir + '/wgs84_repro.shp ' + tmp_dir + '/' + base_file
    os.system(command)

    # prepare in memory file with wgs 84 converted files
    data_dict = {
        'base_file': File(open(tmp_dir + "/wgs84_repro.shp")),
        'shx_file': File(open(tmp_dir + "/wgs84_repro.shx")),

        'dbf_file': File(open(tmp_dir + "/wgs84_repro.dbf")),

        'prj_file': File(open(tmp_dir + "/wgs84_repro.prj")),
    }

    for file in os.listdir(tmp_dir):
        if file.endswith(".xml"):
            new_name = "wgs84_repro.xml"
            os.rename(tmp_dir + '/' + file, tmp_dir + '/' + new_name)

            if File(open(tmp_dir + "/wgs84_repro.xml")):
                data_dict['xml_file'] = File(open(tmp_dir + "/wgs84_repro.xml"))
        """
        elif file.endswith(".sbx"):
            new_name = "wgs84_repro.sbx"
            os.rename(tmp_dir + '/' + file, tmp_dir + '/' + new_name)
            if File(open(tmp_dir + "/wgs84_repro.sbx")):
                data_dict['sbx_file'] = File(open(tmp_dir + "/wgs84_repro.sbx"))

        elif file.endswith(".sbn"):
            new_name = "wgs84_repro.sbn"
            os.rename(tmp_dir + '/' + file, tmp_dir + '/' + new_name)
            if File(open(tmp_dir + "/wgs84_repro.sbn")):
                data_dict['sbn_file'] = File(open(tmp_dir + "/wgs84_repro.sbn"))
        """

    return data_dict


def create_tmp_dir():
    tmp_folder_name = ''.join(random.sample(string.lowercase + string.digits, 10))
    tmp_dir = settings.TEMP_DIR + "/" + tmp_folder_name
    if not os.path.exists(tmp_dir):
        os.makedirs(tmp_dir)

    return tmp_dir


def upload_files(tmp_dir, FILES):
    for file in FILES:
        with open(tmp_dir + '/' + str(FILES[file].name), 'wb+') as destination:
            for chunk in FILES[file].chunks():
                destination.write(chunk)

    # renaming the .shp.xml to .xml
    for file in os.listdir(tmp_dir):
        if file.endswith(".xml"):
            new_name = file.replace('.shp', '')
            os.rename(tmp_dir + '/' + file, tmp_dir + '/' + new_name)


def checking_projection(tmp_dir, file_name):
    prj_open = open(tmp_dir + "/" + file_name, 'r')
    prj = prj_open.readline()
    srs = osr.SpatialReference(wkt=prj)

    return srs


def collect_epsg(tmp_dir, prj_file_name):

    command = "gdalsrsinfo -o proj4 " + tmp_dir + "/" + prj_file_name
    proj4_txt = subprocess.check_output(command, shell=True)
    proj4_txt = proj4_txt.replace('\n', '').replace("'", '')

    """
    select * from spatial_ref_sys
    where
    proj4text = '+proj=longlat +datum=WGS84 +no_defs '

    """
    if proj4_txt:

        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM spatial_ref_sys WHERE proj4text = %s", [proj4_txt])
            result = cursor.fetchall()

        epsg_code = ''
        if len(result) == 0:
            # send error message
            # There is no epsg code for data
            msg = _('Given data is not valid. Please upload valid data.')
            logger.exception(msg)
            raise GeoNodeException(msg)
        elif len(result) >= 1:
            # if more than one result the select first one
            epsg_code = result[0][2]

    return epsg_code


def file_size_with_ext(file_size):
    # if file_size < 1024:
    #     f_size = str(file_size) + ' Byte'
    # elif file_size < 1048576:
    #     f_size = str(round(file_size / 1024.0, 2)) + ' KB'
    # elif file_size < 1073741824:
    #     f_size = str(round(file_size / 1048576.0, 2)) + ' MB'
    # else:
    #     f_size = str(round(file_size / 1073741824.0, 2)) + ' GB'

    return file_size


class SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def get_epsg_code(file):
    prj_file = open(file, 'r')
    request_string = "http://prj2epsg.org/search.json?terms=" + prj_file.read()
    prj_file.close()
    try:
        r = requests.get(request_string)
    except Exception as e:
        return ''
    else:
        if r.status_code != 200:
            return ''

        if len(r.json()['codes']) == 1:
            return r.json()['codes'][0]['code']
        else:
            return ''