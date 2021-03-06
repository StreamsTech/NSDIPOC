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

import json
import time

# @jahangir091
import os
import sys
import logging
import traceback
import shutil
import datetime
# end

from django.conf.urls import url
from django.contrib.auth import get_user_model
from django.core.urlresolvers import reverse
from django.contrib.contenttypes.models import ContentType
from django.conf import settings
from django.db.models import Count
from django.utils.translation import get_language

# @jahangir091
from django.http import HttpResponse
from django.utils.html import escape
from django.shortcuts import get_object_or_404
from django.utils import timezone
# end

from avatar.templatetags.avatar_tags import avatar_url
from guardian.shortcuts import get_objects_for_user
from notify.signals import notify

# @jahangir091
from slugify import slugify
from notify.signals import notify
from user_messages.models import UserThread
from taggit.models import Tag
from django.core.serializers.json import DjangoJSONEncoder
from tastypie.serializers import Serializer
from tastypie import fields
from tastypie.resources import ModelResource
from tastypie.constants import ALL, ALL_WITH_RELATIONS
from tastypie.utils import trailing_slash
from guardian.models import UserObjectPermission
from notify.models import Notification
from user_messages.models import Message
from oauth2_provider.models import AccessToken
from django.contrib.auth import authenticate
from guardian.shortcuts import get_anonymous_user
from oauthlib.common import generate_token
from oauth2_provider.models import Application
from oauth2_provider.generators import generate_client_id, generate_client_secret
from oauth2_provider.settings import oauth2_settings
# end

from geonode.base.models import ResourceBase
from geonode.base.models import TopicCategory
from geonode.base.models import Region
from geonode.base.models import HierarchicalKeyword
from geonode.base.models import ThesaurusKeywordLabel

from geonode.layers.models import Layer, LayerVersionModel
from geonode.maps.models import Map
from geonode.documents.models import Document
from geonode.groups.models import GroupProfile

# @jahangir091
from geonode.groups.models import GroupMember
from geonode.layers.forms import NewLayerUploadForm
from geonode.layers.utils import file_upload
from geonode.layers.models import UploadSession
from geonode.people.models import Profile
from geonode.settings import MEDIA_ROOT
from geonode.maps.models import WmsServer
from geonode.security.views import _perms_info, _perms_info_json
from .authorization import GeoNodeAuthorization
from geonode.base.models import FavoriteResource, DockedResource
from geonode.layers.models import Attribute
from geonode.layers.views import backupCurrentVersion, backupAciveLayer
from geonode.geoserver.helpers import gs_catalog, cascading_delete
from geonode.layers.utils import unzip_file
from  geonode.layers.utils import file_size_with_ext
from geonode.layers.models import LayerSubmissionActivity, LayerAuditActivity
from geonode.documents.models import DocumentSubmissionActivity, DocumentAuditActivity
from geonode.maps.models import MapSubmissionActivity, MapAuditActivity
from geonode.layers.views import save_geometry_type
from django.db.models import Count
CONTEXT_LOG_FILE = None

from utils import sendMailToCommitteeMembers, sendMailToOrganizationAdmin, sendMailToResourceOwner

if 'geonode.geoserver' in settings.INSTALLED_APPS:
    from geonode.geoserver.helpers import _render_thumbnail
    from geonode.geoserver.helpers import ogc_server_settings

    CONTEXT_LOG_FILE = ogc_server_settings.LOG_FILE


def log_snippet(log_file):
    if not os.path.isfile(log_file):
        return "No log file at %s" % log_file


logger = logging.getLogger("geonode.layers.views")
# end


FILTER_TYPES = {
    'layer': Layer,
    'map': Map,
    'document': Document
}


class CountJSONSerializer(Serializer):
    """Custom serializer to post process the api and add counts"""

    def get_resources_counts(self, options):
        if settings.SKIP_PERMS_FILTER:
            resources = ResourceBase.objects.all()
        else:
            resources = get_objects_for_user(
                options['user'],
                'base.view_resourcebase'
            )
        if settings.RESOURCE_PUBLISHING:
            resources = resources.filter(is_published=True)

        if options['title_filter']:
            resources = resources.filter(title__icontains=options['title_filter'])

        if options['type_filter']:
            resources = resources.instance_of(options['type_filter'])

        counts = list(resources.values(options['count_type']).annotate(count=Count(options['count_type'])))

        return dict([(c[options['count_type']], c['count']) for c in counts])

    def to_json(self, data, options=None):
        options = options or {}
        data = self.to_simple(data, options)
        counts = self.get_resources_counts(options)
        if 'objects' in data:
            for item in data['objects']:
                item['count'] = counts.get(item['id'], 0)
        # Add in the current time.
        data['requested_time'] = time.time()

        return json.dumps(data, cls=DjangoJSONEncoder, sort_keys=True)


class TypeFilteredResource(ModelResource):
    """ Common resource used to apply faceting to categories, keywords, and
    regions based on the type passed as query parameter in the form
    type:layer/map/document"""

    count = fields.IntegerField()

    def build_filters(self, filters=None):
        if filters is None:
            filters = {}
        self.type_filter = None
        self.title_filter = None

        orm_filters = super(TypeFilteredResource, self).build_filters(filters)

        if 'type' in filters and filters['type'] in FILTER_TYPES.keys():
            self.type_filter = FILTER_TYPES[filters['type']]
        else:
            self.type_filter = None
        if 'title__icontains' in filters:
            self.title_filter = filters['title__icontains']

        return orm_filters

    def serialize(self, request, data, format, options=None):
        if options is None:
            options = {}
        options['title_filter'] = getattr(self, 'title_filter', None)
        options['type_filter'] = getattr(self, 'type_filter', None)
        options['user'] = request.user

        if 'HTTP_AUTHORIZATION' in request.META:
            token = request.META['HTTP_AUTHORIZATION']
            accesstoken = AccessToken.objects.filter(token=token[7:])
            if accesstoken:
                accesstoken = AccessToken.objects.get(token=token[7:])
                if not accesstoken.is_expired():
                    accesstoken.expires = datetime.datetime.now() + datetime.timedelta(
                        seconds=oauth2_settings.ACCESS_TOKEN_EXPIRE_SECONDS)
                    accesstoken.save()

        return super(TypeFilteredResource, self).serialize(request, data, format, options)


class TagResource(TypeFilteredResource):
    """Tags api"""

    def serialize(self, request, data, format, options=None):
        if options is None:
            options = {}
        options['count_type'] = 'keywords'

        return super(TagResource, self).serialize(request, data, format, options)

    class Meta:
        queryset = HierarchicalKeyword.objects.all().order_by('name')
        resource_name = 'keywords'
        allowed_methods = ['get']
        filtering = {
            'slug': ALL,
        }
        serializer = CountJSONSerializer()


class ThesaurusKeywordResource(TypeFilteredResource):
    """ThesaurusKeyword api"""

    thesaurus_identifier = fields.CharField(null=False)
    label_id = fields.CharField(null=False)

    def build_filters(self, filters={}):
        """adds filtering by current language"""

        id = filters.pop('id', None)

        orm_filters = super(ThesaurusKeywordResource, self).build_filters(filters)

        if id is not None:
            orm_filters['keyword__id'] = id

        orm_filters['lang'] = filters['lang'] if 'lang' in filters else get_language()

        if 'thesaurus' in filters:
            orm_filters['keyword__thesaurus__identifier'] = filters['thesaurus']

        return orm_filters

    def serialize(self, request, data, format, options={}):
        options['count_type'] = 'tkeywords__id'

        return super(ThesaurusKeywordResource, self).serialize(request, data, format, options)

    def dehydrate_id(self, bundle):
        return bundle.obj.keyword.id

    def dehydrate_label_id(self, bundle):
        return bundle.obj.id

    def dehydrate_thesaurus_identifier(self, bundle):
        return bundle.obj.keyword.thesaurus.identifier

    class Meta:
        queryset = ThesaurusKeywordLabel.objects \
            .all() \
            .order_by('label') \
            .select_related('keyword') \
            .select_related('keyword__thesaurus')

        resource_name = 'thesaurus/keywords'
        allowed_methods = ['get']
        filtering = {
            'id': ALL,
            'label': ALL,
            'lang': ALL,
            'thesaurus': ALL,
        }
        serializer = CountJSONSerializer()


class RegionResource(TypeFilteredResource):
    """Regions api"""

    def serialize(self, request, data, format, options=None):
        if options is None:
            options = {}
        options['count_type'] = 'regions'

        return super(RegionResource, self).serialize(request, data, format, options)

    class Meta:
        queryset = Region.objects.all().order_by('name')
        resource_name = 'regions'
        allowed_methods = ['get']
        filtering = {
            'name': ALL,
            'code': ALL,
        }
        if settings.API_INCLUDE_REGIONS_COUNT:
            serializer = CountJSONSerializer()


class TopicCategoryResource(TypeFilteredResource):
    """Category api"""

    def serialize(self, request, data, format, options=None):
        if options is None:
            options = {}
        options['count_type'] = 'category'

        return super(TopicCategoryResource, self).serialize(request, data, format, options)

    class Meta:
        queryset = TopicCategory.objects.all()
        resource_name = 'categories'
        allowed_methods = ['get']
        filtering = {
            'identifier': ALL,
        }
        serializer = CountJSONSerializer()


class GroupResource(ModelResource):
    """Groups api"""

    detail_url = fields.CharField()
    member_count = fields.IntegerField()
    manager_count = fields.IntegerField()

    def dehydrate_member_count(self, bundle):
        return bundle.obj.member_queryset().count()

    def dehydrate_manager_count(self, bundle):
        return bundle.obj.get_managers().count()

    def dehydrate_detail_url(self, bundle):
        return reverse('group_detail', args=[bundle.obj.slug])

    class Meta:
        queryset = GroupProfile.objects.all().exclude(slug='working-group')
        resource_name = 'groups'
        allowed_methods = ['get']
        filtering = {
            'name': ALL,
            'docked': ALL,
            'favorite': ALL,
            'title': ALL
        }
        ordering = ['title', 'last_modified']

    def get_object_list(self, request):

        user_groups = request.GET.get('user-groups')
        if user_groups:
            return super(GroupResource, self).get_object_list(request).filter(groupmember__user=request.user)
        else:
            return super(GroupResource, self).get_object_list(request).all()



class ProfileResource(TypeFilteredResource):
    """Profile api"""

    avatar_100 = fields.CharField(null=True)
    profile_detail_url = fields.CharField()
    email = fields.CharField(default='')
    layers_count = fields.IntegerField(default=0)
    maps_count = fields.IntegerField(default=0)
    documents_count = fields.IntegerField(default=0)
    current_user = fields.BooleanField(default=False)
    activity_stream_url = fields.CharField(null=True)
    section = fields.CharField()
    role = fields.CharField()


    def build_filters(self, filters=None):
        """adds filtering by group functionality"""
        if filters is None:
            filters = {}

        orm_filters = super(ProfileResource, self).build_filters(filters)

        if 'group' in filters:
            orm_filters['group'] = filters['group']

        if 'committee' in filters:
            orm_filters['committee'] = filters['committee']

        return orm_filters

    def apply_filters(self, request, applicable_filters):
        """filter by group if applicable by group functionality"""

        group = applicable_filters.pop('group', None)
        committee = applicable_filters.pop('committee', None)
        semi_filtered = super(
            ProfileResource,
            self).apply_filters(
            request,
            applicable_filters)

        if group is not None:
            semi_filtered = semi_filtered.filter(
                groupmember__group__slug=group)

        if committee:
            semi_filtered = semi_filtered.filter(
                is_working_group_admin=True)

        return semi_filtered

    def dehydrate_email(self, bundle):
        email = ''
        if bundle.request.user.is_authenticated():
            email = bundle.obj.email
        return email

    def dehydrate_layers_count(self, bundle):
        obj_with_perms = get_objects_for_user(bundle.request.user,
                                              'base.view_resourcebase').instance_of(Layer)
        return bundle.obj.resourcebase_set.filter(id__in=obj_with_perms.values('id')).distinct().count()

    def dehydrate_maps_count(self, bundle):
        obj_with_perms = get_objects_for_user(bundle.request.user,
                                              'base.view_resourcebase').instance_of(Map)
        return bundle.obj.resourcebase_set.filter(id__in=obj_with_perms.values('id')).distinct().count()

    def dehydrate_documents_count(self, bundle):
        obj_with_perms = get_objects_for_user(bundle.request.user,
                                              'base.view_resourcebase').instance_of(Document)
        return bundle.obj.resourcebase_set.filter(id__in=obj_with_perms.values('id')).distinct().count()

    def dehydrate_avatar_100(self, bundle):
        return avatar_url(bundle.obj, 240)

    def dehydrate_profile_detail_url(self, bundle):
        return bundle.obj.get_absolute_url()

    def dehydrate_current_user(self, bundle): 
        return bundle.request.user.username == bundle.obj.username

    def dehydrate_role(self, bundle):
        return 'Organization Admin' if bundle.obj.is_manager_of_any_group else 'Member'
    
    def dehydrate_section(self, bundle):
        return bundle.obj.section.title if bundle.obj.section else None

    def dehydrate_activity_stream_url(self, bundle):
        return reverse(
            'actstream_actor',
            kwargs={
                'content_type_id': ContentType.objects.get_for_model(
                    bundle.obj).pk,
                'object_id': bundle.obj.pk})

    def prepend_urls(self):
        if settings.HAYSTACK_SEARCH:
            return [
                url(r"^(?P<resource_name>%s)/search%s$" % (
                    self._meta.resource_name, trailing_slash()
                ),
                    self.wrap_view('get_search'), name="api_get_search"),
            ]
        else:
            return []

    def serialize(self, request, data, format, options=None):
        if options is None:
            options = {}
        options['count_type'] = 'owner'

        return super(ProfileResource, self).serialize(request, data, format, options)

    # @jahangir091
    def get_object_list(self, request):
        if request.user.is_superuser:
            return super(ProfileResource, self).get_object_list(request).exclude(is_staff=True).annotate(max_weight=Count('resourcebase__layer')).order_by('-max_weight')
        else:
            return super(ProfileResource, self).get_object_list(request).filter(is_active=True).exclude(is_staff=True).annotate(max_weight=Count('resourcebase__layer')).order_by('-max_weight')
            # end

    class Meta:
        queryset = get_user_model().objects.exclude(username='AnonymousUser')
        resource_name = 'profiles'
        allowed_methods = ['get']
        ordering = ['username', 'date_joined']
        excludes = ['is_staff', 'password', 'is_superuser',
                    'is_active', 'last_login']

        filtering = {
            'id': ALL,
            'username': ALL,
        }
        serializer = CountJSONSerializer()


class OwnersResource(TypeFilteredResource):
    """Owners api, lighter and faster version of the profiles api"""

    def serialize(self, request, data, format, options=None):
        if options is None:
            options = {}
        options['count_type'] = 'owner'

        return super(OwnersResource, self).serialize(request, data, format, options)

    class Meta:
        queryset = get_user_model().objects.exclude(username='AnonymousUser')
        resource_name = 'owners'
        allowed_methods = ['get']
        ordering = ['username', 'date_joined']
        excludes = ['is_staff', 'password', 'is_superuser',
                    'is_active', 'last_login']

        filtering = {
            'username': ALL,
        }
        serializer = CountJSONSerializer()


# @jahangir091
class UserOrganizationList(TypeFilteredResource):
    group = fields.ForeignKey(GroupResource, 'group', full=True)
    user = fields.ForeignKey(ProfileResource, 'user')

    class Meta:
        queryset = GroupMember.objects.all()
        resource_name = 'user-organization-list'
        filtering = {
            'user': ALL_WITH_RELATIONS
        }


class LayerUpload(TypeFilteredResource):
    class Meta:
        resource_name = 'layerupload'
        allowed_methods = ['post']

    def dispatch(self, request_type, request, **kwargs):
        if request.method == 'POST':
            username = request.GET.get('username') or request.POST.get('username')
            password = request.GET.get('password') or request.POST.get('password')
            out = {'success': False}
            try:
                user = Profile.objects.get(username=username)
            except Profile.DoesNotExist:
                out['errors'] = 'The username and/or password you specified are not correct.'
                return HttpResponse(json.dumps(out), content_type='application/json', status=404)

            if user.check_password(password):
                request.user = user
            else:
                out['errors'] = 'The username and/or password you specified are not correct.'
                return HttpResponse(json.dumps(out), content_type='application/json', status=404)
            form = NewLayerUploadForm(request.POST, request.FILES)
            tempdir = None
            errormsgs = []
            if form.is_valid():
                title = form.cleaned_data["layer_title"]
                category = form.cleaned_data["category"]
                organization_id = form.cleaned_data["organization"]
                try:
                    group = GroupProfile.objects.get(id=organization_id)
                except GroupProfile.DoesNotExist:
                    out['errors'] = 'Organization does not exists'
                    return HttpResponse(json.dumps(out), content_type='application/json', status=404)
                else:
                    if not group in group.groups_for_user(request.user):
                        out['errors'] = 'Organization access denied'
                        return HttpResponse(json.dumps(out), content_type='application/json', status=404)
                # Replace dots in filename - GeoServer REST API upload bug
                # and avoid any other invalid characters.
                # Use the title if possible, otherwise default to the filename
                if title is not None and len(title) > 0:
                    name_base = title
                    keywords = title.split()
                else:
                    name_base, __ = os.path.splitext(
                        form.cleaned_data["base_file"].name)
                name = slugify(name_base.replace(".", "_"))
                try:
                    # Moved this inside the try/except block because it can raise
                    # exceptions when unicode characters are present.
                    # This should be followed up in upstream Django.
                    tempdir, base_file = form.write_files()
                    saved_layer = file_upload(
                        base_file,
                        name=name,
                        user=request.user,
                        category=category,
                        group=group,
                        keywords=keywords,
                        status='ACTIVE',
                        overwrite=False,
                        # charset=form.cleaned_data["charset"],
                        abstract=form.cleaned_data["abstract"],
                        title=form.cleaned_data["layer_title"],
                        metadata_uploaded_preserve=form.cleaned_data["metadata_uploaded_preserve"]
                    )
                    file_size, file_type = form.get_type_and_size()
                    f_size = file_size_with_ext(file_size)
                    saved_layer.file_size = f_size
                    saved_layer.file_type = file_type
                    saved_layer.current_version = 1
                    saved_layer.save()
                except Exception as e:
                    exception_type, error, tb = sys.exc_info()
                    logger.exception(e)
                    out['success'] = False
                    out['errors'] = str(error)
                    # Assign the error message to the latest UploadSession from that user.
                    latest_uploads = UploadSession.objects.filter(user=request.user).order_by('-date')
                    if latest_uploads.count() > 0:
                        upload_session = latest_uploads[0]
                        upload_session.error = str(error)
                        upload_session.traceback = traceback.format_exc(tb)
                        upload_session.context = log_snippet(CONTEXT_LOG_FILE)
                        upload_session.save()
                        out['traceback'] = upload_session.traceback
                        out['context'] = upload_session.context
                        out['upload_session'] = upload_session.id
                else:
                    out['success'] = True
                    if hasattr(saved_layer, 'info'):
                        out['info'] = saved_layer.info
                    out['url'] = reverse(
                        'layer_detail', args=[
                            saved_layer.service_typename])
                    upload_session = saved_layer.upload_session
                    upload_session.processed = True
                    upload_session.save()

                    # save geometry type for the uploaded layer(point, line, polygone)
                    geometry_type = save_geometry_type(saved_layer)
                    saved_layer.geometry_type = geometry_type
                    saved_layer.save()


                    # permissions = form.cleaned_data["permissions"]
                    # permissions = str(permissions)
                    # if permissions is not None and len(permissions.keys()) > 0:
                    #     saved_layer.set_permissions(permissions)
                finally:
                    if tempdir is not None:
                        shutil.rmtree(tempdir)
            else:
                for e in form.errors.values():
                    errormsgs.extend([escape(v) for v in e])
                out['errors'] = form.errors
                out['errormsgs'] = errormsgs
            if out['success']:
                status_code = 200
            else:
                status_code = 400
            return HttpResponse(json.dumps(out), content_type='application/json', status=status_code)


class MakeFeatured(TypeFilteredResource):
    class Meta:
        resource_name = 'make-featured'
        allowed_methods = ['post']

    def dispatch(self, request_type, request, **kwargs):
        if request.method == 'POST':
            out = {'success': False}
            user = request.user
            if user.is_authenticated() and user.is_manager_of_any_group:
                status = json.loads(request.body).get('status')
                resource_id = json.loads(request.body).get('resource_id')

                try:
                    layer = Layer.objects.get(pk=resource_id)
                    resource = ResourceBase.objects.get(pk=resource_id)
                except ResourceBase.DoesNotExist:
                    status_code = 404
                    out['errors'] = 'Layer does not exist'
                else:
                    if layer.group in user.group_list_all():
                        resource.featured = status
                        if status == True:
                            permissions = _perms_info_json(layer)
                            perm_dict = json.loads(permissions)
                            try:
                                if 'download_resourcebase' in perm_dict['users']['AnonymousUser']:
                                    perm_dict['users']['AnonymousUser'].remove('download_resourcebase')
                            except:
                                pass

                            try:
                                if 'download_resourcebase' in perm_dict['groups']['anonymous']:
                                    perm_dict['groups']['anonymous'].remove('download_resourcebase')
                            except:
                                pass

                            layer.set_permissions(perm_dict)

                        resource.save()
                        out['success'] = 'True'
                        status_code = 200
                    else:
                        out['error'] = 'Access denied'
                        out['success'] = False
                        status_code = 400
            else:
                out['error'] = 'Access denied'
                out['success'] = False
                status_code = 400
            return HttpResponse(json.dumps(out), content_type='application/json', status=status_code)


class MesseagesUnread(TypeFilteredResource):
    class Meta:
        resource_name = 'message-unread'
        queryset = UserThread.objects.filter(unread=True)
        allowed_methods = ['get']

    def get_object_list(self, request):
        return super(MesseagesUnread, self).get_object_list(request).filter(user=request.user)


class UndockResources(TypeFilteredResource):
    class Meta:
        resource_name = 'undockit'
        allowed_methods = ['post']

    def dispatch(self, request_type, request, **kwargs):
        if request.method == 'POST':
            out = {'success': False}
            user = request.user
            if user.is_authenticated():
                resource_id = json.loads(request.body).get('resource_id')
                group_id = json.loads(request.body).get('group_id')
                if resource_id:
                    try:
                        resource = ResourceBase.objects.get(pk=resource_id)
                    except ResourceBase.DoesNotExist:
                        status_code = 404
                        out['errors'] = 'resource does not exist'
                    else:
                        docked = DockedResource.objects.get(user=user, resource=resource)
                        docked.active = False
                        docked.save()
                        out['success'] = 'True'
                        status_code = 200

                elif group_id:
                    try:
                        group = GroupProfile.objects.get(pk=group_id)
                    except ResourceBase.DoesNotExist:
                        status_code = 404
                        out['errors'] = 'group does not exist'
                    else:
                        docked = DockedResource.objects.get(user=user, group=group)
                        docked.active = False
                        docked.save()
                        out['success'] = 'True'
                        status_code = 200
            else:
                out['error'] = 'Access denied'
                out['success'] = False
                status_code = 400
            return HttpResponse(json.dumps(out), content_type='application/json', status=status_code)


class FavoriteUnfavoriteResources(TypeFilteredResource):
    class Meta:
        resource_name = 'makefavorite'

    def dispatch(self, request_type, request, **kwargs):
        if request.method == 'POST':
            out = {'success': False}
            user = request.user
            if user.is_authenticated():
                status = json.loads(request.body).get('status')
                resource_id = json.loads(request.body).get('resource_id')
                group_id = json.loads(request.body).get('group_id')

                if resource_id:
                    try:
                        resource = ResourceBase.objects.get(pk=resource_id)
                    except ResourceBase.DoesNotExist:
                        status_code = 404
                        out['errors'] = 'resource does not exist'
                    else:
                        favorite, created = FavoriteResource.objects.get_or_create(user=user, resource=resource)
                        docked, created = DockedResource.objects.get_or_create(user=user, resource=resource)
                        favorite.active = status
                        docked.active = status
                        favorite.save()
                        docked.save()
                        out['success'] = 'True'
                        status_code = 200

                elif group_id:
                    try:
                        group = GroupProfile.objects.get(pk=group_id)
                    except ResourceBase.DoesNotExist:
                        status_code = 404
                        out['errors'] = 'group does not exist'
                    else:
                        favorite, created = FavoriteResource.objects.get_or_create(user=user, group=group)
                        docked, created = DockedResource.objects.get_or_create(user=user, group=group)
                        favorite.active = status
                        docked.active = status
                        favorite.save()
                        docked.save()
                        out['success'] = 'True'
                        status_code = 200
            else:
                out['error'] = 'Access denied'
                out['success'] = False
                status_code = 400
            return HttpResponse(json.dumps(out), content_type='application/json', status=status_code)


class OsmOgrInfo(TypeFilteredResource):
    class Meta:
        resource_name = 'ogrinfo'
        allowed_methods = ['post']

    def dispatch(self, request_type, request, **kwargs):
        if request.method == 'POST':
            out = {'success': False}
            user = request.user
            if user.is_authenticated():
                try:
                    file = request.FILES["base_file"]
                except:
                    out['errors'] = 'No file has been choosen as base_file'
                    return HttpResponse(json.dumps(out), content_type='application/json', status=404)
                else:
                    filename = file.name
                    extension = os.path.splitext(filename)[1]
                    if extension.lower() != '.osm':
                        out['errors'] = 'Please upload a valid .osm file'
                        return HttpResponse(json.dumps(out), content_type='application/json', status=404)

                    file_location = os.path.join(MEDIA_ROOT, "osm_temp")
                    temporary_file = open('%s/%s' % (file_location, filename), 'w+')
                    temporary_file.write(file.read())
                    temporary_file.close()
                    file_path = temporary_file.name
                    from plumbum.cmd import ogrinfo
                    output_string = ogrinfo(file_path)
                    point_layer = '(Point)'
                    line_layer = '(Line String)'
                    multi_line_layer = '(Multi Line String)'
                    multipolygon_layer = '(Geometry Collection)'
                    if point_layer in output_string:
                        out['points'] = 'points'
                    if line_layer in output_string:
                        out['lines'] = 'lines'
                    if multi_line_layer in output_string:
                        out['multilinestrings'] = 'multilinestrings'
                    if multipolygon_layer in output_string:
                        out['multipolygons'] = 'multipolygons'
                    out['success'] = True

                    os.remove(file_path)
                    return HttpResponse(json.dumps(out), content_type='application/json', status=200)


class LayerSourceServer(TypeFilteredResource):
    """
    api for retrieving server info for layer source
    """

    class Meta:
        resource_name = 'layersource'
        allowed_methods = ['get']
        queryset = WmsServer.objects.all()


class MetaFavorite:
    authorization = GeoNodeAuthorization()
    allowed_methods = ['get']
    ordering = ['date', 'title', 'popular_count']
    fields = [
        'id',
        'uuid',
        'title',
        'date',
        'abstract',
        'csw_wkt_geometry',
        'csw_type',
        'owner__username',
        'share_count',
        'popular_count',
        'srid',
        'category__gn_description',
        'supplemental_information',
        'thumbnail_url',
        'detail_url',
        'rating',
        'featured',
        'resource_type',
        'download_count',

    ]


class LayersWithFavoriteAndDoocked(TypeFilteredResource):
    class Meta(MetaFavorite):
        queryset = Layer.objects.filter(favoriteresource__active=True, status='ACTIVE').order_by('-date')
        if settings.RESOURCE_PUBLISHING:
            queryset = queryset.filter(is_published=True)
        resource_name = 'favoritelayers'
        allowed_methods = ['get']

    def get_object_list(self, request):
        return super(LayersWithFavoriteAndDoocked, self).get_object_list(request).filter(
            favoriteresource__user=request.user, dockedresource__active=True).distinct()


class MapsWithFavoriteAndDoocked(TypeFilteredResource):
    class Meta(MetaFavorite):
        queryset = Map.objects.filter(favoriteresource__active=True, status='ACTIVE').order_by('-date')
        if settings.RESOURCE_PUBLISHING:
            queryset = queryset.filter(is_published=True)

        resource_name = 'favoritemaps'
        allowed_methods = ['get']

    def get_object_list(self, request):
        return super(MapsWithFavoriteAndDoocked, self).get_object_list(request).filter(
            favoriteresource__user=request.user, dockedresource__active=True).distinct()


class GroupsWithFavoriteAndDoocked(TypeFilteredResource):
    detail_url = fields.CharField()

    def dehydrate_detail_url(self, bundle):
        return reverse('group_detail', args=[bundle.obj.slug])

    class Meta:
        queryset = GroupProfile.objects.filter(favoriteresource__active=True).exclude(slug='working-group')
        if settings.RESOURCE_PUBLISHING:
            queryset = queryset.filter(is_published=True)

        resource_name = 'favoritegroups'
        allowed_methods = ['get']

    def get_object_list(self, request):
        return super(GroupsWithFavoriteAndDoocked, self).get_object_list(request).filter(
            favoriteresource__user=request.user, dockedresource__active=True).distinct()


class DocumentsWithFavoriteAndDoocked(TypeFilteredResource):
    class Meta(MetaFavorite):
        queryset = Document.objects.filter(favoriteresource__active=True, status='ACTIVE').order_by('-date')
        if settings.RESOURCE_PUBLISHING:
            queryset = queryset.filter(is_published=True)

        resource_name = 'favoritedocuments'
        allowed_methods = ['get']

    def get_object_list(self, request):
        return super(DocumentsWithFavoriteAndDoocked, self).get_object_list(request).filter(
            favoriteresource__user=request.user, dockedresource__active=True).distinct()


class UserNotifications(TypeFilteredResource):
    class Meta:
        queryset = Notification.objects.filter(read=False, deleted=False)
        resource_name = 'admin_notifications'

    def get_object_list(self, request):
        timestamp = request.GET.get('timestamp')
        if timestamp:
            date = datetime.datetime.fromtimestamp(float(timestamp))
            return super(UserNotifications, self).get_object_list(request).filter(recipient=request.user,
                                                                                  created__gt=date.date())
        else:
            return super(UserNotifications, self).get_object_list(request).filter(recipient=request.user)


class ViewNotificationTimeSaving(TypeFilteredResource):
    class Meta:
        queryset = Notification.objects.filter(read=False, deleted=False)
        resource_name = 'view-notification'
        allowed_methods = ['get']

    def get_object_list(self, request):
        user = request.user
        user.last_notification_view = timezone.now()
        user.save()
        return super(ViewNotificationTimeSaving, self).get_object_list(request).filter(recipient=user,
                                                                                       created__gt=user.last_notification_view)


# end

#
# class AccessTokenApi(TypeFilteredResource):
#     def dehydrate_expires(self, bundle):
#         return oauth2_settings.ACCESS_TOKEN_EXPIRE_SECONDS
#
#     class Meta:
#         queryset = AccessToken.objects.all()
#         resource_name = 'access-token'
#         allowed_methods = ['get', 'post']
#         fields = ['token', 'expires']
#
#     def get_object_list(self, request):
#         username = request.META['HTTP_USER']
#         password = request.META['HTTP_PASSWORD']
#         user = authenticate(username=username, password=password)
#         if user is not None and user.is_active:
#             if not super(AccessTokenApi, self).get_object_list(request).filter(user=user):
#                 createToken(user)
#             else:
#                 accesstoken = AccessToken.objects.filter(user=user).first()
#                 if accesstoken.expires < datetime.datetime.now():
#                     createToken(user)
#             return super(AccessTokenApi, self).get_object_list(request).filter(user=user)
#         else:
#             user = get_anonymous_user()
#             return super(AccessTokenApi, self).get_object_list(request).filter(user=user)


class AccessTokenApi(TypeFilteredResource):
    """
    This api returns access token for an user.
    This access token is needed for token based authentiction.
    it takes body parameters:
        'user'
        'password'
    """

    class Meta:
        resource_name = 'access-token'
        list_allowed_methods = ['post']

    def dispatch(self, request_type, request, **kwargs):
        if request.method == 'POST':
            out = {'success': False}
            username = str(json.loads(request.body).get('user'))
            password = str(json.loads(request.body).get('password'))
            user = authenticate(username=username, password=password)
            if user is not None and user.is_active:
                user_access_tokens = AccessToken.objects.filter(user=user, expires__gte = datetime.datetime.now())
                if user_access_tokens:
                    token = user_access_tokens.latest('expires')
                else:
                    token = createToken(user)

                out['success'] = True
                out['token'] = token.token
                out['expires'] = (token.expires - datetime.datetime.now()).total_seconds()
                status_code = 200

            else:
                out['error'] = 'Access denied'
                out['success'] = False
                status_code = 400
            return HttpResponse(json.dumps(out), content_type='application/json', status=status_code)


def getApplication(user):
    user_apps = Application.objects.filter(user=user)

    if user_apps.exists():
        return user_apps.get(user=user)
    else:

        client_id = generate_client_id()
        client_secret = generate_client_secret()

        app = Application.objects.create(
            skip_authorization=True,
            redirect_uris=ogc_server_settings.public_url,
            name=user.username,
            authorization_grant_type='password',
            client_type='confidential',
            client_id=client_id,
            client_secret=client_secret,
            user=user
        )
        app.save()
        return app


def createToken(user):
    # Lets create a new one
    # accesstoken = AccessToken.objects.filter(user=user)
    # if accesstoken:
    #     accessntoken = AccessToken.objects.get(user=user)
    #     accesstoken.delete()
    token = generate_token()
    app = getApplication(user)
    access_token = AccessToken.objects.create(user=user,
                               application=app,
                               expires=datetime.datetime.now() + datetime.timedelta(
                                   seconds=oauth2_settings.ACCESS_TOKEN_EXPIRE_SECONDS),
                               token=token)
    return access_token




class LayerDownloadCountApi(TypeFilteredResource):

    class Meta:
        resource_name = 'layer-download-count'

    def dispatch(self, request_type, request, **kwargs):
        if request.method == 'POST':
            out = {'success': False}

            layer_id = json.loads(request.body).get('layer_id')

            if layer_id:
                try:
                    layer = Layer.objects.get(id=layer_id)
                except Layer.DoesNotExist:
                    status_code = 404
                    out['errors'] = 'layer does not exist'
                else:
                    layer.download_count = layer.download_count + 1
                    layer.save()
                    out['success'] = 'True'
                    status_code = 200

            else:
                out['error'] = 'Access denied'
                out['success'] = False
                status_code = 400
            return HttpResponse(json.dumps(out), content_type='application/json', status=status_code)



class LayerPermissionPreviewApi(TypeFilteredResource):

    class Meta:
        resource_name = 'layer-attribute-permission-set'
        allowed_methods = ['post']
        authorization = GeoNodeAuthorization()

    def dispatch(self, request_type, request, **kwargs):
        out = {'success': False}
        if not request.user.is_authenticated():
            out['errors'] = 'User is not authenticated'
            return HttpResponse(json.dumps(out), content_type='application/json', status=200)

        if request.method == 'POST':
            out = {'success': False}
            layer_pk = json.loads(request.body).get('layer_pk')
            permissions = json.loads(request.body).get('permissions')
            attributes = json.loads(request.body).get('attributes')
            status = json.loads(request.body).get('status')
            comment_body = json.loads(request.body).get('comment')
            comment_subject = json.loads(request.body).get('comment_subject')
            try:
                layer = Layer.objects.get(id=layer_pk)
            except:
                out['errors'] = 'No layer found with this layer pk'
                return HttpResponse(json.dumps(out), content_type='application/json', status=200)

            if request.user.is_working_group_admin and status in ["ACTIVE", "DENIED"]:
                layer_submission_activity = LayerSubmissionActivity.objects.get(
                    layer=layer, group=layer.group, iteration=layer.current_iteration)
                layer_audit_activity = LayerAuditActivity(
                    layer_submission_activity=layer_submission_activity)
                layer.status = status
                layer.last_auditor = request.user
                layer.save()

                verb = ""
                if status == "ACTIVE":
                    verb = "approved"
                elif status == "DENIED":
                    verb = "denied"

                # notify layer owner that someone have approved the layer
                notify.send(request.user, recipient=layer.owner, actor=request.user,
                                target=layer, verb='{0} your layer'.format(verb))

                # if organization admin/committee member verifies/denies the layer then send a mail to the layer owner
                # and organization admin informing that admin has verified/denied the requested layer
                sendMailToResourceOwner(layer_pk, 'layer', status)
                sendMailToOrganizationAdmin(layer_pk, 'layer')

                layer_submission_activity.is_audited = True
                layer_submission_activity.save()

                layer_audit_activity.comment_subject = comment_subject or "Great work"
                layer_audit_activity.comment_body = comment_body or "Layer verified"
                layer_audit_activity.result = status
                layer_audit_activity.auditor = request.user
                layer_audit_activity.save()

            elif request.user in layer.group.get_managers() and status in ["VERIFIED", "DENIED"]:
                layer_submission_activity = LayerSubmissionActivity.objects.get(
                    layer=layer, group=layer.group, iteration=layer.current_iteration)

                #create a new audit activity
                layer_audit_activity = LayerAuditActivity(
                    layer_submission_activity=layer_submission_activity)

                #set layer status
                layer.status = status
                layer.last_auditor = request.user
                layer.save()

                # set permissions
                if permissions is not None and len(permissions.keys()) > 0:
                    layer.set_permissions(permissions)

                # set attribute level prmissions
                layer_attributes = Attribute.objects.filter(layer=layer)
                for attr in layer_attributes:
                    if attr.id in attributes:
                        attr.is_permitted = True
                    else:
                        attr.is_permitted = False

                    attr.save()



                layer_submission_activity.is_audited = True
                layer_submission_activity.save()

                layer_audit_activity.comment_subject = comment_subject or "Great work"
                layer_audit_activity.comment_body = comment_body or "Layer verified"

                if status == "VERIFIED":
                    # set working group admins permissions for this layer
                    w_group = GroupProfile.objects.get(slug='working-group')
                    layer.set_working_group_permissions(group=w_group)
                    layer_audit_activity.result = 'APPROVED'
                if status == "DENIED":
                    layer_audit_activity.result = 'DECLINED'

                layer_audit_activity.auditor = request.user
                layer_audit_activity.save()

                # notify layer owner that manager have verified/denied the layer
                notify.send(request.user, recipient=layer.owner, actor=request.user,
                            target=layer, verb='{0} your layer'.format(status))

                # if the layer is verified, then inform working group
                # admins that a new layr has been sent to him for approval
                if status == "VERIFIED":
                    working_group_admins = Profile.objects.filter(is_working_group_admin=True)
                    notify.send(request.user, recipient_list=list(working_group_admins), actor=request.user,
                                target=layer, verb='pushed a new layer for approval')
                    sendMailToCommitteeMembers(layer.id, 'layer')

                # if organization admin verifies/denies the layer then send a mail to the layer owner
                # informing that admin has verified/denied the requested layer
                sendMailToResourceOwner(layer_pk, 'layer', status)



            elif request.user ==  layer.owner and status == "PENDING":

                #set layer status
                layer.status = status
                #increase iteration
                layer.current_iteration += 1
                #save layer
                layer.save()
                #create layer submission activity
                layer_submission_activity = LayerSubmissionActivity(
                    layer=layer, group=layer.group, iteration=layer.current_iteration)
                layer_submission_activity.save()

                #set permissions
                if permissions is not None and len(permissions.keys()) > 0:
                    layer.set_permissions(permissions)

                #set attribute level prmissions
                layer_attributes = Attribute.objects.filter(layer=layer)
                for attr in layer_attributes:
                    if attr.id in attributes:
                        attr.is_permitted = True
                    else:
                        attr.is_permitted = False

                    attr.save()

                #set manager permissions for this layer
                layer.set_managers_permissions(manager=layer.group.get_managers().first())

                # notify organization admin about the new published layer
                notify.send(request.user, recipient=layer.group.get_managers().first(), actor=request.user,
                                verb='pushed a new layer for verification', target=layer)

                # send email to organization admin that a new layer has been sent for verification
                sendMailToOrganizationAdmin(layer_pk, 'layer')

                out['success'] = True

            else:
                out['errors'] = 'You dont have permission to perform this action'
        else:
            out['errors'] = 'Only post method is permitted'
        return HttpResponse(json.dumps(out), content_type='application/json', status=200)


class MapPermissionPreviewApi(TypeFilteredResource):

    class Meta:
        resource_name = 'map-attribute-permission-set'
        allowed_methods = ['post']
        authorization = GeoNodeAuthorization()

    def dispatch(self, request_type, request, **kwargs):
        out = {'success': False}
        if not request.user.is_authenticated():
            out['errors'] = 'User is not authenticated'
            return HttpResponse(json.dumps(out), content_type='application/json', status=200)

        if request.method == 'POST':
            out = {'success': False}
            map_pk = json.loads(request.body).get('resource_pk')
            permissions = json.loads(request.body).get('permissions')
            # attributes = json.loads(request.body).get('attributes')
            status = json.loads(request.body).get('status')
            comment_body = json.loads(request.body).get('comment')
            comment_subject = json.loads(request.body).get('comment_subject')
            try:
                map = Map.objects.get(id=map_pk)
            except:
                out['errors'] = 'No map found with this map pk'
                return HttpResponse(json.dumps(out), content_type='application/json', status=200)

            if request.user.is_working_group_admin and status in ["ACTIVE", "DENIED"]:
                map_submission_activity = MapSubmissionActivity.objects.get(
                    map=map, group=map.group, iteration=map.current_iteration)
                map_audit_activity = MapAuditActivity(
                    map_submission_activity=map_submission_activity)
                map.status = status
                map.last_auditor = request.user
                map.save()

                verb = ""
                if status == "ACTIVE":
                    verb = "approved"
                elif status == "DENIED":
                    verb = "denied"

                # notify map owner that someone have approved the map
                notify.send(request.user, recipient=map.owner, actor=request.user,
                                target=map, verb='{0} your map'.format(verb))

                # if organization admin/committee member verifies/denies the map then send a mail to the map owner
                # and organization admin informing that admin has verified/denied the requested map
                sendMailToResourceOwner(map_pk, 'map', status)
                sendMailToOrganizationAdmin(map_pk, 'map')

                map_submission_activity.is_audited = True
                map_submission_activity.save()

                map_audit_activity.comment_subject = comment_subject or "Great work"
                map_audit_activity.comment_body = comment_body or "Map verified"
                map_audit_activity.result = status
                map_audit_activity.auditor = request.user
                map_audit_activity.save()

            elif request.user in map.group.get_managers() and status in ["VERIFIED", "DENIED"]:
                map_submission_activity = MapSubmissionActivity.objects.get(
                    map=map, group=map.group, iteration=map.current_iteration)

                #create a new audit activity
                map_audit_activity = MapAuditActivity(
                    map_submission_activity=map_submission_activity)

                #set map status
                map.status = status
                map.last_auditor = request.user
                map.save()

                # set permissions
                if permissions is not None and len(permissions.keys()) > 0:
                    map.set_permissions(permissions)

                map_submission_activity.is_audited = True
                map_submission_activity.save()

                map_audit_activity.comment_subject = comment_subject or "Great work"
                map_audit_activity.comment_body = comment_body or "Map verified"

                if status == "VERIFIED":
                    # set working group admins permissions for this map
                    w_group = GroupProfile.objects.get(slug='working-group')
                    map.set_working_group_permissions(group=w_group)
                    map_audit_activity.result = 'APPROVED'
                if status == "DENIED":
                    map_audit_activity.result = 'DECLINED'

                map_audit_activity.auditor = request.user
                map_audit_activity.save()

                # notify map owner that manager have verified the map
                notify.send(request.user, recipient=map.owner, actor=request.user,
                            target=map, verb='{0} your map'.format(status))


                #if the map is verified, the inform working group
                #admins to approve this map
                if status == "VERIFIED":
                    working_group_admins = Profile.objects.filter(is_working_group_admin=True)
                    notify.send(request.user, recipient_list=list(working_group_admins), actor=request.user,
                                target=map, verb='pushed a new map for approval')
                    sendMailToCommitteeMembers(map.id, 'map')

                # if organization admin verifies/denies the map then send a mail to the map owner
                # informing that admin has verified/denied the requested map
                sendMailToResourceOwner(map_pk, 'map', status)


            elif request.user ==  map.owner and status == "PENDING":

                #set map status
                map.status = status
                #increase iteration
                map.current_iteration += 1
                #save map
                map.save()
                #create map submission activity
                map_submission_activity = MapSubmissionActivity(
                    map=map, group=map.group, iteration=map.current_iteration)
                map_submission_activity.save()

                #set permissions
                if permissions is not None and len(permissions.keys()) > 0:
                    map.set_permissions(permissions)

                #set manager permissions for this map
                    map.set_managers_permissions(manager=map.group.get_managers().first())

                # notify organization admin about the new published map
                notify.send(request.user, recipient=map.group.get_managers().first(), actor=request.user,
                                verb='pushed a new map for verification', target=map)

                # send email to organization admin that a new map has been sent for verification
                sendMailToOrganizationAdmin(map_pk, 'map')

                out['success'] = True

            else:
                out['errors'] = 'You dont have permission to perform this action'
        else:
            out['errors'] = 'Only post method is permitted'
        return HttpResponse(json.dumps(out), content_type='application/json', status=200)


class DocumentPermissionPreviewApi(TypeFilteredResource):

    class Meta:
        resource_name = 'document-attribute-permission-set'
        allowed_methods = ['post']
        authorization = GeoNodeAuthorization()

    def dispatch(self, request_type, request, **kwargs):
        out = {'success': False}
        if not request.user.is_authenticated():
            out['errors'] = 'User is not authenticated'
            return HttpResponse(json.dumps(out), content_type='application/json', status=200)

        if request.method == 'POST':
            out = {'success': False}
            document_pk = json.loads(request.body).get('resource_pk')
            permissions = json.loads(request.body).get('permissions')
            attributes = json.loads(request.body).get('attributes')
            status = json.loads(request.body).get('status')
            comment_body = json.loads(request.body).get('comment')
            comment_subject = json.loads(request.body).get('comment_subject')
            try:
                document = Document.objects.get(id=document_pk)
            except:
                out['errors'] = 'No document found with this document pk'
                return HttpResponse(json.dumps(out), content_type='application/json', status=200)

            if request.user.is_working_group_admin and status in ["ACTIVE", "DENIED"]:
                document_submission_activity = DocumentSubmissionActivity.objects.get(
                    document=document, group=document.group, iteration=document.current_iteration)
                document_audit_activity = DocumentAuditActivity(
                    document_submission_activity=document_submission_activity)
                document.status = status
                document.last_auditor = request.user
                document.save()

                verb = ""
                if status == "ACTIVE":
                    verb = "approved"
                elif status == "DENIED":
                    verb = "denied"

                # notify document owner that someone have approved the document
                notify.send(request.user, recipient=document.owner, actor=request.user,
                                target=document, verb='{0} your document'.format(verb))

                # if organization admin/committee member verifies/denies the document then send a mail to the document owner
                # and organization admin informing that admin has verified/denied the requested document
                sendMailToResourceOwner(document_pk, 'document', status)
                sendMailToOrganizationAdmin(document_pk, 'document')

                document_submission_activity.is_audited = True
                document_submission_activity.save()

                document_audit_activity.comment_subject = comment_subject or "Great work"
                document_audit_activity.comment_body = comment_body or "Document verified"
                document_audit_activity.result = status
                document_audit_activity.auditor = request.user
                document_audit_activity.save()

            elif request.user in document.group.get_managers() and status in ["VERIFIED", "DENIED"]:
                document_submission_activity = DocumentSubmissionActivity.objects.get(
                    document=document, group=document.group, iteration=document.current_iteration)

                #create a new audit activity
                document_audit_activity = DocumentAuditActivity(
                    document_submission_activity=document_submission_activity)

                #set document status
                document.status = status
                document.last_auditor = request.user
                document.save()

                # set permissions
                if permissions is not None and len(permissions.keys()) > 0:
                    document.set_permissions(permissions)

                document_submission_activity.is_audited = True
                document_submission_activity.save()

                document_audit_activity.comment_subject = comment_subject or "Great work"
                document_audit_activity.comment_body = comment_body or "Document verified"

                if status == "VERIFIED":
                    # set working group admins permissions for this document
                    w_group = GroupProfile.objects.get(slug='working-group')
                    document.set_working_group_permissions(group=w_group)
                    document_audit_activity.result = 'APPROVED'
                if status == "DENIED":
                    document_audit_activity.result = 'DECLINED'

                document_audit_activity.auditor = request.user
                document_audit_activity.save()

                # notify document owner that manager have verified the document
                notify.send(request.user, recipient=document.owner, actor=request.user,
                            target=document, verb='{0} your document'.format(status))

                #if the document is verified, the inform working group
                #admins to approve this document
                if status == "VERIFIED":
                    working_group_admins = Profile.objects.filter(is_working_group_admin=True)
                    notify.send(request.user, recipient_list=list(working_group_admins), actor=request.user,
                                target=document, verb='pushed a new document for approval')
                    sendMailToCommitteeMembers(document.id, 'document')

                # if organization admin verifies/denies the document then send a mail to the document owner
                # informing that admin has verified/denied the requested document
                sendMailToResourceOwner(document_pk, 'document', status)


            elif request.user ==  document.owner and status == "PENDING":

                #set document status
                document.status = status
                #increase iteration
                document.current_iteration += 1
                #save document
                document.save()
                #create document submission activity
                document_submission_activity = DocumentSubmissionActivity(
                    document=document, group=document.group, iteration=document.current_iteration)
                document_submission_activity.save()

                #set permissions
                if permissions is not None and len(permissions.keys()) > 0:
                    document.set_permissions(permissions)

                #set manager permissions for this document
                    document.set_managers_permissions(manager=document.group.get_managers().first())

                # notify organization admin about the new published document
                notify.send(request.user, recipient=document.group.get_managers().first(), actor=request.user,
                                verb='pushed a new document for verification', target=document)

                # send email to organization admin that a new document has been sent for verification
                sendMailToOrganizationAdmin(document_pk, 'document')

                out['success'] = True

            else:
                out['errors'] = 'You dont have permission to perform this action'
        else:
            out['errors'] = 'Only post method is permitted'
        return HttpResponse(json.dumps(out), content_type='application/json', status=200)


class ResourcePermissionPreviewApi(TypeFilteredResource):
    class Meta:
        resource_name = 'resource-attribute-permission-set'
        allowed_methods = ['post']
        authorization = GeoNodeAuthorization()

    def dispatch(self, request_type, request, **kwargs):
        out = {'success': False}
        if not request.user.is_authenticated():
            out['errors'] = 'User is not authenticated'
            return HttpResponse(json.dumps(out), content_type='application/json', status=200)

        if request.method == 'POST':
            out = {'success': False}

            resource_pk = json.loads(request.body).get('resource_pk')
            permissions = json.loads(request.body).get('permissions')
            attributes = json.loads(request.body).get('attributes')
            status = json.loads(request.body).get('status')

            try:
                resource = ResourceBase.objects.get(id=resource_pk)
            except:
                out['errors'] = 'Invalid resource id'
                return HttpResponse(json.dumps(out), content_type='application/json', status=200)

            if resource.resource_type == 'layer':
                target_resource = Layer.objects.get(id=resource_pk)
                verb = 'approved your layer'

            elif resource.resource_type == 'map':
                target_resource = Map.objects.get(id=resource_pk)
                verb = 'approved your map'
            elif resource.resource_type == 'document':
                target_resource = Document.objects.get(id=resource_pk)
                verb = 'approved your document'

            if request.user.is_working_group_admin or request.user == target_resource.owner:

                if request.user.is_working_group_admin:
                    notify.send(request.user, recipient=target_resource.owner, actor=request.user,
                                target=target_resource, verb=verb)

                target_resource.status = status
                target_resource.save()
                if permissions is not None and len(permissions.keys()) > 0:
                    target_resource.set_permissions(permissions)

                w_group = GroupProfile.objects.get(slug='working-group')
                resource.set_working_group_permissions(w_group)

                if attributes and target_resource.resource_type == 'layer':
                    layer_attributes = Attribute.objects.filter(layer=target_resource)
                    for attr in layer_attributes:
                        if attr.id in attributes:
                            attr.is_permitted = True
                        else:
                            attr.is_permitted = False
                        attr.save()

                out['success'] = True


            else:
                out['errors'] = 'You dont have permission to update permissions'
        else:
            out['errors'] = 'Only post method is permitted'
        return HttpResponse(json.dumps(out), content_type='application/json', status=200)


class ChangeLayerVersionApi(TypeFilteredResource):
    """
    This api changes layer version
    if there is multiple available versions of a layer
    then user can select any version and then
    the layer will changed to that selected version
    and the pevious or current version will be archaived as
    an odler version
    it takes body parameters:
        'layer_id'
        'version_id'
        'user_id'
    """

    class Meta:
        resource_name = 'change-layer-version-api'
        list_allowed_methods = ['post']

    def dispatch(self, request_type, request, **kwargs):
        if request.method == 'POST':
            out = {'success': False}

            layer_id = json.loads(request.body).get('layer_id')
            layer_version_id = json.loads(request.body).get('version_id')

            if layer_id and layer_version_id:
                try:
                    layer = Layer.objects.get(pk=layer_id)
                except Layer.DoesNotExist:
                    status_code = 404
                    out['errors'] = 'layer does not exist'
                else:

                    # backupCurrentVersion(layer)
                    backupAciveLayer(layer)
                    selected_version_model = LayerVersionModel.objects.get(id=layer_version_id)

                    cat = gs_catalog
                    cascading_delete(cat, layer.typename)

                    base_file = unzip_file(selected_version_model.version_path, '.shp', tempdir=None)

                    saved_layer = file_upload(
                        base_file,
                        name=layer.name,
                        user=request.user,
                        overwrite=True,
                        # charset=form.cleaned_data["charset"],
                    )
                    # file_size, file_type = form.get_type_and_size()
                    # saved_layer.file_size = file_size
                    # saved_layer.file_type = file_type
                    saved_layer.current_version = selected_version_model.version
                    saved_layer.save()
                    out['success'] = True
                    out['url'] = reverse(
                        'layer_detail', args=[
                            saved_layer.service_typename])

                    out['success'] = 'True'
                    status_code = 200

            else:
                out['error'] = 'Access denied'
                out['success'] = False
                status_code = 400
            return HttpResponse(json.dumps(out), content_type='application/json', status=status_code)




class SetBaseLayerAPI(TypeFilteredResource):
    """
    This api sets layers as base layer
    it takes body parameters:
        'layer_ids'
    """

    class Meta:
        resource_name = 'set-layers-base-layer'
        list_allowed_methods = ['post']

    def dispatch(self, request_type, request, **kwargs):
        if request.method == 'POST':
            out = {'success': False}

            if request.user.is_authenticated() and request.user.is_superuser:

                layer_ids = json.loads(request.body).get('layer_ids')

                layers = Layer.objects.all()
                for layer in layers:
                    if layer.id in layer_ids:
                        layer.is_base_layer = True
                        layer.save()
                    else:
                        if layer.is_base_layer:
                            layer.is_base_layer = False
                            layer.save()

                        out['success'] = 'True'
                        status_code = 200

            else:
                out['error'] = 'Access denied'
                out['success'] = False
                status_code = 400
            return HttpResponse(json.dumps(out), content_type='application/json', status=status_code)



class LayerMapDocumentApproveDenyAPI(TypeFilteredResource):
    """
    This api sets layers as base layer
    it takes body parameters:
        'layer_ids'
    """

    class Meta:
        resource_name = 'layer-map-documet-approve-deny'
        list_allowed_methods = ['post']

    def dispatch(self, request_type, request, **kwargs):
        if request.method == 'POST':
            out = {'success': False}

            if request.user.is_authenticated():
                resource_type = json.loads(request.body).get('resource_type')
                resource_pk = json.loads(request.body).get('resource_pk')
                action = json.loads(request.body).get('action')
                resource = False
                if resource_type == 'layer':
                    try:
                        resource = Layer.objects.get(id=resource_pk)
                    except Layer.DoesNotExist:
                        out['error'] = 'requested layer does not exists'
                        out['success'] = False
                        status_code = 400

                elif resource_type == 'map':
                    try:
                        resource = Map.objects.get(id=resource_pk)
                    except Layer.DoesNotExist:
                        out['error'] = 'requested map does not exists'
                        out['success'] = False
                        status_code = 400

                elif resource_type == 'document':
                    try:
                        resource = Document.objects.get(id=resource_pk)
                    except Layer.DoesNotExist:
                        out['error'] = 'requested document does not exists'
                        out['success'] = False
                        status_code = 400

                if resource:
                    group = resource.group
                    if request.user not in group.get_managers():
                        out['error'] = 'You are not allowed to approve this resource'
                        out['success'] = False
                        status_code = 400
                    else:
                        if resource_type == 'layer':
                            resource_submission_activity = LayerSubmissionActivity.objects.get(
                            layer=resource, group=group, iteration=resource.current_iteration)
                            resource_audit_activity = LayerAuditActivity(
                                layer_submission_activity=resource_submission_activity)
                        elif resource_type == 'map':
                            resource_submission_activity = MapSubmissionActivity.objects.get(
                                map=resource, group=group, iteration=resource.current_iteration)
                            resource_audit_activity = MapAuditActivity(
                                map_submission_activity=resource_submission_activity)
                        elif resource_type == 'document':
                            resource_submission_activity = DocumentSubmissionActivity.objects.get(
                                document=resource, group=group, iteration=resource.current_iteration)
                            resource_audit_activity = DocumentAuditActivity(
                                document_submission_activity=resource_submission_activity)


                        comment_body = json.loads(request.body).get('comment')
                        comment_subject = json.loads(request.body).get('comment_subject')
                        if action.upper() == 'APPROVED':
                            resource.status = 'ACTIVE'
                        elif action.upper() == 'DENIED':
                            resource.status = 'DENIED'
                        resource.last_auditor = request.user
                        resource.save()

                        permissions = _perms_info_json(resource)
                        perm_dict = json.loads(permissions)
                        if json.loads(request.body).get('view_permission'):
                            if not 'AnonymousUser' in perm_dict['users']:
                                perm_dict['users']['AnonymousUser'] = []
                                perm_dict['users']['AnonymousUser'].append(
                                    'view_resourcebase')
                            else:
                                if not 'view_resourcebase' in perm_dict['users']['AnonymousUser']:
                                    perm_dict['users']['AnonymousUser'].append(
                                        'view_resourcebase')

                        if json.loads(request.body).get('download_permission'):
                            if not 'AnonymousUser' in perm_dict['users']:
                                perm_dict['users']['AnonymousUser'] = []
                                perm_dict['users']['AnonymousUser'].append(
                                    'download_resourcebase')
                            else:
                                if not 'download_resourcebase' in perm_dict['users']['AnonymousUser']:
                                    perm_dict['users']['AnonymousUser'].append(
                                        'download_resourcebase')

                        resource.set_permissions(perm_dict)

                        # notify layer owner that someone have approved the layer
                        if request.user != resource.owner:
                            recipient = resource.owner
                            notify.send(request.user, recipient=recipient, actor=request.user,
                                        target=resource, verb=action + ' your ' + resource_type)

                        resource_submission_activity.is_audited = True
                        resource_submission_activity.save()

                        resource_audit_activity.comment_subject = comment_subject
                        resource_audit_activity.comment_body = comment_body
                        resource_audit_activity.result = resource.status
                        resource_audit_activity.auditor = request.user
                        resource_audit_activity.save()

                        out['success'] = 'True'
                        out['message'] = 'Approved Resource Successfully'
                        status_code = 200


            else:
                out['error'] = 'Access denied'
                out['success'] = False
                status_code = 400
            return HttpResponse(json.dumps(out), content_type='application/json', status=status_code)

