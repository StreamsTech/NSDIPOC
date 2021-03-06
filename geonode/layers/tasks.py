import time
import os
import requests
import shutil
import logging
import hashlib


from django.core import serializers
from django.conf import settings
from django.core.mail import send_mail

try:
    import json
except ImportError:
    from django.utils import simplejson as json


from geonode.base.models import ResourceBase
from geonode.layers.models import Layer
from geonode.settings import MEDIA_ROOT
from geonode.groups.models import GroupProfile
from geonode.people.models import Profile

from geonode.geoserver.helpers import cascading_delete, gs_catalog
from geonode.layers.utils import unzip_file
from geonode.layers.utils import file_upload
from geonode.nsdi.utils import get_organization
from geonode.api.tasks import send_task_email


db_logger = logging.getLogger('db')
now = time.time()



from celery import shared_task


def backupOneLayer(layer, temdir):
    download_link = layer.link_set.get(name='Zipped Shapefile')
    r = requests.get(download_link.url)
    zfile = open(temdir + '/' + layer.name + '.zip', 'wb')
    zfile.write(r.content)
    zfile.close()
    r.close()


@shared_task
def backupOrganizationLayersMetadata( host, user_id):

    user = Profile.objects.get(id=user_id)
    organization = get_organization(user)
    # organization = GroupProfile.objects.get(id=organization_id)


    layer_objects = Layer.objects.filter(group=organization)
    resource_base_objects = ResourceBase.objects.filter(group=organization)
    jsonSerializer = serializers.get_serializer("json")
    json_serializer = jsonSerializer()
    all_objects = list(layer_objects) + list(resource_base_objects)

    temdir = MEDIA_ROOT + '/backup/organization/' + organization.slug
    if not os.path.exists(temdir):
        os.makedirs(temdir)
    metadata_location = temdir + '/metadata.txt'
    with open(metadata_location, "w") as out:
        json_serializer.serialize(all_objects, stream=out)

    for l in layer_objects:
        backupOneLayer(l, temdir)

    send_mail_to_admin(host, organization, temdir, user)
    return '{} layers backed up with success!'.format(layer_objects.count())


def send_mail_to_admin(host, organization, temdir, user):
    hash_str = str(organization.slug) + "_" + str(now)

    zip_file_name = hashlib.sha224(hash_str).hexdigest()
    # zip_file_name += MEDIA_ROOT + '/backup/organization/'
    shutil.make_archive(MEDIA_ROOT + '/backup/organization/' + zip_file_name, 'zip', temdir)
    shutil.rmtree(temdir)
    org_download_link = "http://" +  host + "/uploaded/backup/organization/" + zip_file_name + ".zip"

    # Send email
    subject = 'Download Organizations Layers'
    from_email = settings.EMAIL_FROM
    recipient_list = [str(user.email)]  # str(request.user.email)

    # html_message = "<a href='" + org_download_link + "'>Please go to the following link to download organizations layers:</a> <br/><br/><br/>" + org_download_link
    f = open("geonode/templates/org_layers_download_mail_template.html", "r")
    html_message = f.read()
    f.close()
    html_message = html_message.format(org_download_link, org_download_link)

    try:
        send_task_email.delay(subject, html_message, from_email, recipient_list)
    except Exception as e:
        db_logger.exception(e)



@shared_task
def restoreOrganizationLayersMetadata(metadata_file):
    # with open("/home/streamstech/bk/or2/metadata.txt", "r") as out:
    with open(metadata_file, "r") as out:
        data = out.read()

    for obj in serializers.deserialize("json", data):
        obj.save()



@shared_task
def restoreLayer(layer, file_path):
    cat = gs_catalog
    cascading_delete(cat, layer.typename)
    # file_path = '/home/streamstech/nsdipoc/geonode/uploaded/backup/organization/or2/vcvc.zip'

    base_file = unzip_file(file_path, '.shp', tempdir=None)
    user = layer.owner
    saved_layer = file_upload(
        base_file,
        name=layer.name,
        user=user,
        overwrite=True,
        # charset=form.cleaned_data["charset"],
    )
    # saved_layer.save()