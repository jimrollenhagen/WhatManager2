import base64
from itertools import count
import json
import os
import pickle
import re
import socket
from time import sleep

from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.db import models
from django.db.models.aggregates import Sum
from django.utils import timezone
from django.utils.functional import cached_property
import requests
import transmissionrpc

from WhatManager2 import settings
from WhatManager2.settings import FREELEECH_EMAIL_TO, WHAT_CD_DOMAIN, FREELEECH_HOSTNAME, FREELEECH_EMAIL_FROM
from WhatManager2.utils import match_properties, copy_properties, norm_t_torrent, html_unescape, wm_str, get_artists
from home.info_holder import InfoHolder


class TorrentAlreadyAddedException(Exception):
    pass


class ReplicaSet(models.Model):
    ZONE_WHAT = 'what.cd'
    ZONE_BIBLIOTIK = 'bibliotik.org'

    zone = models.CharField(max_length=16)
    name = models.TextField()

    def __unicode__(self):
        return u'ReplicaSet({0}, {1})'.format(self.zone, self.name)

    @property
    def is_master(self):
        return self.name == 'master'

    @property
    def torrent_count(self):
        return sum(i.torrent_count for i in self.transinstance_set.all())

    @property
    def torrents_size(self):
        return sum(i.torrents_size for i in self.transinstance_set.all())

    def get_preferred_instance(self):
        return sorted(self.transinstance_set.all(), key=lambda x: x.torrent_count)[0]

    @classmethod
    def get_what_master(cls):
        return cls.objects.get(zone=cls.ZONE_WHAT, name='master')

    @classmethod
    def get_bibliotik_master(cls):
        return cls.objects.get(zone=cls.ZONE_BIBLIOTIK, name='master')


class DownloadLocation(models.Model):
    zone = models.CharField(max_length=16)
    path = models.TextField()
    preferred = models.BooleanField()

    def __unicode__(self):
        return u'DownloadLocation({0}, {1}{2})'.format(
            self.zone, self.path, ', preferred' if self.preferred else '')

    @cached_property
    def disk_space(self):
        stat = os.statvfs(self.path)
        return {
            'free': stat.f_bfree * stat.f_bsize,
            'used': (stat.f_blocks - stat.f_bfree) * stat.f_bsize,
            'total': stat.f_blocks * stat.f_bsize
        }

    @cached_property
    def free_space_percent(self):
        return float(self.disk_space['free']) / self.disk_space['total']


    @classmethod
    def get_what_preferred(cls):
        return DownloadLocation.objects.get(
            zone=ReplicaSet.ZONE_WHAT,
            preferred=True,
        )


    @classmethod
    def get_bibliotik_preferred(cls):
        return DownloadLocation.objects.get(
            zone=ReplicaSet.ZONE_BIBLIOTIK,
            preferred=True,
        )


    @cached_property
    def torrent_count(self):
        replica_sets = ReplicaSet.objects.filter(name='master')
        instances = TransInstance.objects.filter(replica_set__in=replica_sets)
        return (
            self.transtorrent_set.filter(instance__in=instances).count() +
            self.bibliotiktranstorrent_set.filter(instance__in=instances).count()
        )


    @classmethod
    def get_by_full_path(cls, full_path):
        for d in DownloadLocation.objects.all():
            if full_path.startswith(d.path):
                return d
        return None


class TransInstance(models.Model):
    class Meta:
        permissions = (
            ('view_transinstance_stats', 'Can view current Transmission stats.'),
            ('run_checks', 'Can run the validity checks.'),
        )

    replica_set = models.ForeignKey(ReplicaSet)
    name = models.TextField()
    host = models.TextField()
    port = models.IntegerField()
    peer_port = models.IntegerField()
    username = models.TextField()
    password = models.TextField()

    def __unicode__(self):
        return u'TransInstance {0}({1}@{2}:{3})'.format(self.name, self.username, self.host, self.port)

    def full_description(self):
        return u'TransInstance {0}(replica_set={1}, host={2}, rpc_port={3}, peer_port={4}, username={5}, password={6})'.format(
            self.name, self.replica_set, self.host, self.port, self.peer_port, self.username, self.password
        )

    @property
    def client(self):
        if not hasattr(self, '_client'):
            self._client = transmissionrpc.Client(address=self.host, port=self.port, user=self.username,
                                                  password=self.password)
        return self._client

    @property
    def torrent_count(self):
        if self.replica_set.zone == ReplicaSet.ZONE_WHAT:
            return self.transtorrent_set.count()
        elif self.replica_set.zone == ReplicaSet.ZONE_BIBLIOTIK:
            return self.bibliotiktranstorrent_set.count()

    @property
    def torrents_size(self):
        if self.replica_set.zone == ReplicaSet.ZONE_WHAT:
            return self.transtorrent_set.aggregate(Sum('torrent_size'))['torrent_size__sum']
        elif self.replica_set.zone == ReplicaSet.ZONE_BIBLIOTIK:
            return self.bibliotiktranstorrent_set.aggregate(Sum('torrent_size'))['torrent_size__sum']

    def get_t_torrents(self, arguments):
        torrents = []
        locations = DownloadLocation.objects.filter(zone=self.replica_set.zone)
        if not u'downloadDir' in arguments:
            arguments.append(u'downloadDir')
        for t in self.client.get_torrents(arguments=arguments):
            if any([l for l in locations if t.downloadDir.startswith(l.path)]):
                norm_t_torrent(t)
                torrents.append(t)
        return torrents

    def get_t_torrents_by_hash(self, arguments):
        torrents = {}
        for t in self.get_t_torrents(arguments):
            torrents[t.hashString] = t
        return torrents

    def get_m_torrents_by_hash(self):
        torrents = {}
        for t in self.transtorrent_set.all():
            if t.info_hash in torrents and t.what_torrent_id == torrents[t.info_hash].what_torrent_id:
                t.delete()
                continue
            torrents[t.info_hash] = t
        return torrents

    def get_b_torrents_by_hash(self):
        torrents = {}
        for t in self.bibliotiktranstorrent_set.all():
            if t.info_hash in torrents and t.bibliotik_torrent_id == torrents[t.info_hash].bibliotik_torrent_id:
                t.delete()
                continue
            torrents[t.info_hash] = t
        return torrents


class WhatFulltext(models.Model):
    info = models.TextField()
    more_info = models.TextField()

    def get_info(self, what_torrent):
        info = json.loads(what_torrent.info)

        info_text = []
        info_text.append(unicode(info['group']['id']))
        info_text.append(info['group']['recordLabel'])
        info_text.append(info['group']['name'])
        info_text.append(info['group']['catalogueNumber'])
        if info['group']['musicInfo']:
            for type, artists in info['group']['musicInfo'].items():
                if artists:
                    artist_names = [a['name'] for a in artists]
                    info_text.append(', '.join(artist_names))
        info_text.append(unicode(info['group']['year']))

        info_text.append(unicode(info['torrent']['id']))
        info_text.append(unicode(info['torrent']['remasterYear']))
        info_text.append(info['torrent']['filePath'])
        info_text.append(info['torrent']['remasterCatalogueNumber'])
        info_text.append(info['torrent']['remasterRecordLabel'])
        info_text.append(info['torrent']['remasterTitle'])

        info_text = '\r\n'.join(info_text)
        info_text = html_unescape(info_text)
        return info_text

    def match(self, what_torrent):
        return (
            self.get_info(what_torrent) == self.info and
            what_torrent.info == self.more_info
        )

    def update(self, what_torrent):
        self.info = self.get_info(what_torrent)
        self.more_info = what_torrent.info
        self.save()

    def __unicode__(self):
        return u'WhatFulltext id={0}'.format(self.id)


class WhatTorrent(models.Model, InfoHolder):
    class Meta:
        permissions = (
            ('view_whattorrent', 'Can view torrents.'),
            ('download_whattorrent', 'Can download and play torrents.'),
        )

    info_hash = models.TextField()
    torrent_file = models.TextField()
    torrent_file_name = models.TextField()
    retrieved = models.DateTimeField()
    info = models.TextField()
    tags = models.TextField()
    added_by = models.ForeignKey(User, null=True)
    what_group_id = models.IntegerField()

    def save(self, *args, **kwargs):
        if self.what_group_id != self.info_loads['group']['id']:
            self.what_group_id = self.info_loads['group']['id']

        super(WhatTorrent, self).save(*args, **kwargs)
        try:
            what_fulltext = WhatFulltext.objects.get(id=self.id)
            if not what_fulltext.match(self):
                what_fulltext.update(self)
        except WhatFulltext.DoesNotExist:
            what_fulltext = WhatFulltext(id=self.id)
            what_fulltext.update(self)

    def delete(self, *args, **kwargs):
        try:
            WhatFulltext.objects.get(id=self.id).delete()
        except WhatFulltext.DoesNotExist:
            pass
        super(WhatTorrent, self).delete(*args, **kwargs)

    def __unicode__(self):
        return u'WhatTorrent id={0} hash={1}'.format(self.id, self.info_hash)

    @cached_property
    def master_trans_torrent(self):
        torrents = list(TransTorrent.objects.filter(
            what_torrent=self,
            instance__in=ReplicaSet.get_what_master().transinstance_set.all()
        ))
        if len(torrents):
            return torrents[0]
        return None

    @cached_property
    def joined_artists(self):
        return get_artists(self.info_loads['group'])

    @property
    def torrent_file_binary(self):
        return base64.b64decode(self.torrent_file)

    @cached_property
    def info_loads(self):
        return json.loads(self.info)

    @staticmethod
    def get_or_none(request, info_hash=None, what_id=None):
        if info_hash and what_id:
            raise Exception(u'Specify one.')
        if not info_hash and not what_id:
            raise Exception(u'Specify one.')
        try:
            if info_hash:
                return WhatTorrent.objects.get(info_hash=info_hash)
            elif what_id:
                return WhatTorrent.objects.get(id=what_id)
        except WhatTorrent.DoesNotExist:
            return None

    @staticmethod
    def is_downloaded(request, info_hash=None, what_id=None):
        w_torrent = WhatTorrent.get_or_none(request, info_hash, what_id)
        if w_torrent and w_torrent.transtorrent_set.count() > 0:
            return True
        return False

    @staticmethod
    def get_or_create(request, info_hash=None, what_id=None):
        if info_hash and what_id:
            raise Exception(u'Specify one.')
        if not info_hash and not what_id:
            raise Exception(u'Specify one.')

        try:
            if info_hash:
                if len(info_hash) != 40:
                    raise Exception(u'Invalid info hash.')
                return WhatTorrent.objects.get(info_hash=info_hash)
            else:
                return WhatTorrent.objects.get(id=what_id)
        except WhatTorrent.DoesNotExist:
            what = get_what_client(request)

            if info_hash:
                data = what.request('torrent', hash=info_hash)['response']
            else:
                data = what.request('torrent', id=what_id)['response']

            filename, torrent = what.get_torrent(data['torrent']['id'])
            w_torrent = WhatTorrent(
                id=int(data['torrent']['id']),
                info_hash=data['torrent']['infoHash'],
                torrent_file=base64.b64encode(torrent),
                torrent_file_name=filename,
                retrieved=timezone.now(),
                info=json.dumps(data)
            )
            w_torrent.save()
            return w_torrent


class TransTorrentBase(models.Model):
    class Meta:
        abstract = True

    sync_t_arguments = [
        'id', 'name', 'hashString', 'totalSize', 'uploadedEver', 'percentDone', 'addedDate',
        'error', 'errorString'
    ]
    sync_t_props = (
        ('torrent_id', 'id'),
        ('torrent_name', 'name'),
        ('torrent_size', 'totalSize'),
        ('torrent_uploaded', 'uploadedEver'),
        ('torrent_done', 'percentDone'),
        ('torrent_date_added', 'date_added_tz'),
        ('torrent_error', 'error'),
        ('torrent_error_string', 'errorString'),
    )

    instance = models.ForeignKey(TransInstance)
    location = models.ForeignKey(DownloadLocation)

    info_hash = models.TextField()
    torrent_id = models.IntegerField(null=True)
    torrent_name = models.TextField(null=True)
    torrent_size = models.BigIntegerField(null=True)
    torrent_uploaded = models.BigIntegerField(null=True)
    torrent_done = models.FloatField(null=True)
    torrent_date_added = models.DateTimeField(null=True)
    torrent_error = models.IntegerField(null=True)
    torrent_error_string = models.TextField(null=True)

    def sync_t_torrent(self, t_torrent=None):
        if t_torrent is None:
            t_torrent = self.instance.client.get_torrent(self.torrent_id, arguments=TransTorrentBase.sync_t_arguments)
            norm_t_torrent(t_torrent)

        if not match_properties(self, t_torrent, TransTorrentBase.sync_t_props):
            copy_properties(self, t_torrent, TransTorrentBase.sync_t_props)
            self.save()


class TransTorrent(TransTorrentBase):
    what_torrent = models.ForeignKey(WhatTorrent)

    @property
    def path(self):
        return os.path.join(self.location.path, unicode(self.what_torrent.id))

    def sync_files(self):
        if os.path.exists(self.path):
            files = [f.decode('utf-8') for f in os.listdir(self.path)]
        else:
            os.mkdir(self.path, 0777)
            os.chmod(self.path, 0777)
            files = []

        files_added = []
        if not any(u'.torrent' in f for f in files):
            files_added.append(u'torrent')
            torrent_path = os.path.join(wm_str(self.path), wm_str(self.what_torrent.torrent_file_name))
            with open(torrent_path, 'wb') as file:
                file.write(self.what_torrent.torrent_file_binary)
            os.chmod(torrent_path, 0777)
        if not any(u'ReleaseInfo2.txt' == f for f in files):
            files_added.append(u'ReleaseInfo2.txt')
            release_info_path = os.path.join(self.path.encode('utf-8'), u'ReleaseInfo2.txt')
            with open(release_info_path.decode('utf-8'), 'w') as file:
                file.write(self.what_torrent.info)
            os.chmod(os.path.join(release_info_path.decode('utf-8')), 0777)
        if files_added:
            LogEntry.add(None, u'info', u'Added files {0} to {1}'.format(', '.join(files_added), self))

    def __unicode__(self):
        return u'TransTorrent(torrent_id={0}, what_id={1}, name={2})'.format(self.torrent_id, self.what_torrent_id,
                                                                             self.torrent_name)


class LogEntry(models.Model):
    class Meta:
        permissions = (
            ('view_logentry', 'Can view the logs.'),
        )

    user = models.ForeignKey(User, null=True, related_name='wm_logentry')
    datetime = models.DateTimeField(auto_now_add=True)
    type = models.TextField()
    message = models.TextField()
    traceback = models.TextField(null=True)

    @staticmethod
    def add(user, log_type, message, traceback=None):
        entry = LogEntry(user=user, type=log_type, message=message, traceback=traceback)
        entry.save()


class WhatLoginCache(models.Model):
    cookies = models.TextField()
    authkey = models.TextField()
    passkey = models.TextField()


headers = {
    'Content-type': 'application/x-www-form-urlencoded',
    'Accept-Charset': 'utf-8',
    'User-Agent': 'whatapi [isaaczafuta]'
}


class LoginException(Exception):
    pass


class RequestException(Exception):
    def __init__(self, message=None, response=None):
        super(Exception, self).__init__(message)
        self.response = response


def send_freeleech_email(message):
    send_mail(u'Freeleech', message, FREELEECH_EMAIL_FROM, [FREELEECH_EMAIL_TO])


class CustomWhatAPI:
    def __init__(self, username=None, password=None):
        self.session = requests.Session()
        self.session.headers = headers
        self.authkey = None
        self.passkey = None
        self.username = username
        self.password = password
        self._login()

    def _login(self):
        try:
            login_cache = WhatLoginCache.objects.get()
            for cookie in pickle.loads(login_cache.cookies):
                self.session.cookies.set_cookie(cookie)
            self.authkey = login_cache.authkey
            self.passkey = login_cache.passkey
            self.request('index')
        except Exception:
            '''Logs in user and gets authkey from server'''
            loginpage = 'https://{0}/login.php'.format(WHAT_CD_DOMAIN)
            data = {'username': self.username,
                    'password': self.password,
                    'keeplogged': 1,
                    'login': 'Login'
            }
            r = self.session.post(loginpage, data=data, allow_redirects=False)
            if r.status_code != 302:
                raise LoginException
            accountinfo = self.request("index")
            self.authkey = accountinfo["response"]["authkey"]
            self.passkey = accountinfo["response"]["passkey"]
            for cache in WhatLoginCache.objects.all():
                cache.delete()
            login_cache = WhatLoginCache(
                cookies=pickle.dumps([c for c in self.session.cookies]),
                authkey=self.authkey,
                passkey=self.passkey
            )
            login_cache.save()

    def request(self, action, **kwargs):
        '''Makes an AJAX request at a given action page'''
        ajaxpage = 'https://{0}/ajax.php'.format(WHAT_CD_DOMAIN)
        params = {'action': action}
        if self.authkey:
            params['auth'] = self.authkey
        params.update(kwargs)

        r = self.session.get(ajaxpage, params=params, allow_redirects=False)
        try:
            json_response = r.json()
            if json_response["status"] != "success":
                raise RequestException(response=json_response)
            return json_response
        except ValueError:
            raise RequestException()

    def get_torrent(self, torrent_id):
        '''Downloads the torrent at torrent_id using the authkey and passkey'''
        torrentpage = 'https://{0}/torrents.php'.format(WHAT_CD_DOMAIN)
        params = {'action': 'download', 'id': torrent_id}
        if self.authkey:
            params['authkey'] = self.authkey
            params['torrent_pass'] = self.passkey
        r = self.session.get(torrentpage, params=params, allow_redirects=False)
        if r.status_code == 200 and 'application/x-bittorrent' in r.headers['content-type']:
            filename = re.search('filename="(.*)"', r.headers['content-disposition']).group(1)
            return filename, r.content
        return None

    def get_free_torrents(self):
        # Start form 1 up
        for page in count(1):
            response = self.request('browse', freetorrent=1, page=page)['response']
            if response['pages'] > 20 and socket.gethostname() == FREELEECH_HOSTNAME:
                send_freeleech_email('Site-wide freeleech.')
                raise Exception('More than 20 pages of free torrents. Site-wide freeleech?')
            for result in response['results']:
                yield result
            if response['currentPage'] == response['pages']:
                break
            sleep(2)

    def get_free_torrent_ids(self):
        for free_group in self.get_free_torrents():
            if 'torrents' in free_group:
                for torrent in free_group['torrents']:
                    if torrent['isFreeleech']:
                        yield int(torrent['torrentId']), free_group, torrent
            else:
                if free_group['isFreeleech']:
                    yield int(free_group['torrentId']), free_group, free_group


def get_what_client(request):
    if not hasattr(request, 'what_client'):
        request.what_client = None
        for i in range(3):
            try:
                request.what_client = CustomWhatAPI(username=settings.WHAT_USERNAME,
                                                    password=settings.WHAT_PASSWORD)
                break
            except RequestException as ex:
                pass
        if request.what_client is None:
            raise ex
    return request.what_client
