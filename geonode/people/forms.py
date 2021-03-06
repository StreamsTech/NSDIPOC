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

import taggit

from django import forms
from django.contrib.auth.forms import UserCreationForm, UserChangeForm
from django.utils.translation import ugettext_lazy as _

from geonode.people.models import Profile
from geonode.base.models import ContactRole
from geonode import settings
from account.models import EmailAddress
from account.forms import SignupForm
from geonode.groups.models import SectionModel
from geonode.groups.views import userOrganizationSections
from utils import getFields

# Ported in from django-registration
attrs_dict = {'class': 'required'}


class ProfileCreationForm(UserCreationForm):

    class Meta:
        model = Profile
        fields = ("username",)

    def clean_username(self):
        # Since User.username is unique, this check is redundant,
        # but it sets a nicer error message than the ORM. See #13147.
        username = self.cleaned_data["username"]
        try:
            Profile.objects.get(username=username)
        except Profile.DoesNotExist:
            return username
        raise forms.ValidationError(
            self.error_messages['duplicate_username'],
            code='duplicate_username',
        )


class ProfileChangeForm(UserChangeForm):

    class Meta:
        model = Profile
        fields = '__all__'


class ForgotUsernameForm(forms.Form):
    email = forms.EmailField(widget=forms.TextInput(attrs=dict(attrs_dict,
                                                               maxlength=75)),
                             label=_(u'Email Address'))


class RoleForm(forms.ModelForm):

    class Meta:
        model = ContactRole
        exclude = ('contact', 'layer')


class PocForm(forms.Form):
    contact = forms.ModelChoiceField(label="New point of contact",
                                     queryset=Profile.objects.all())


class ProfileForm(forms.ModelForm):
    keywords = taggit.forms.TagField(
        required=False,
        help_text=_("A space or comma-separated list of keywords"))

    class Meta:
        model = Profile
        exclude = ['password',
            'last_login',
            'groups',
            'user_permissions',
            'username',
            'is_staff',
            'is_superuser',
            'is_active',
            'date_joined']

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super(ProfileForm, self).__init__(*args, **kwargs)
        #exclude form fields according to user role
        #super admin can edit all the fields, but
        # normal user are allowed to edit limited fields
        if self.user:
            self.fields = getFields(self.user, self.fields)


    def clean_email(self):
        value = self.cleaned_data["email"]
        qs = EmailAddress.objects.filter(email__iexact=value)
        if not qs.exists() or not settings.ACCOUNT_EMAIL_UNIQUE or value==self.instance.email:
            return value
        raise forms.ValidationError(_("A user is registered with this email address."))



class UserSignupFormExtend(SignupForm):
    section = forms.ModelChoiceField(queryset=SectionModel.objects.all(),
                                #   help_text=_('select section for this user'),
                                    label=_('Section/wing/branch'),

                                    )

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super(UserSignupFormExtend, self).__init__(*args, **kwargs)
        self.fields['section'].queryset = userOrganizationSections(self.user)
        self.fields.keyOrder = ['section', 'username', 'password', 'password_confirm', 'email']



class UserSignupFormWithWorkingGroup(SignupForm):
    is_working_group_admin = forms.BooleanField(help_text=_('Select if the user is a member of Committee'),
                                    label=_('Is member of committee?'), required=False)

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super(UserSignupFormWithWorkingGroup, self).__init__(*args, **kwargs)
        self.fields.keyOrder = ['is_working_group_admin', 'username', 'password', 'password_confirm', 'email']
