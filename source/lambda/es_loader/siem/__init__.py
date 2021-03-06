# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import base64
import bz2
import configparser
import gzip
import hashlib
import io
import ipaddress
import json
import os
import re
import zipfile
from datetime import datetime, timedelta, timezone
import boto3
import geoip2.database

__version__ = '2.0.0'


# REGEXP and boot for lambda warm start
# for transform script
re_instanceid = re.compile(r'\W?(?P<instanceid>i-[0-9a-z]{8,17})\W?')
RE_ACCOUNT = re.compile(r'/([0-9]{12})/')
RE_REGION = re.compile('(global|(us|ap|ca|eu|me|sa|af)-[a-zA-Z]+-[0-9])')
# for syslog timestamp
RE_SYSLOG_FORMAT = re.compile(r'([A-Z][a-z]{2})\s+(\d{1,2})\s+'
                              r'(\d{2}):(\d{2}):(\d{2})(\.(\d{1,6}))?')
MONTH_TO_INT = {'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
                'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12}
TD_OFFSET12 = timedelta(hours=12)


# download geoip database
def download_geoip_database(s3key_prefix='GeoLite2/'):
    if 'GEOIP_BUCKET' in os.environ:
        geoipbucket = os.environ.get('GEOIP_BUCKET', '')
    else:
        config = configparser.ConfigParser(
            interpolation=configparser.ExtendedInterpolation())
        config.read('aes.ini')
        config.sections()
        if 'aes' in config:
            geoipbucket = config['aes']['GEOIP_BUCKET']
        else:
            return None
    geoip_dbs = ['GeoLite2-City.mmdb', 'GeoLite2-ASN.mmdb']
    for db in geoip_dbs:
        localfile = '/tmp/' + db
        localfile_not_found = '/tmp/not_found_' + db
        if os.path.isfile(localfile_not_found):
            return True
        if not os.path.isfile(localfile):
            s3geo = boto3.resource('s3')
            bucket = s3geo.Bucket(geoipbucket)
            s3obj = s3key_prefix + db
            try:
                bucket.download_file(s3obj, localfile)
            except Exception:
                print(db + ' is not found in s3')
                with open(localfile_not_found, 'w') as f:
                    f.write('')
    print('These files are in /tmp: ' + str(os.listdir(path='/tmp/')))


download_geoip_database()

reader_city = None
reader_geo = None
if os.path.isfile('/tmp/GeoLite2-City.mmdb'):
    reader_city = geoip2.database.Reader('/tmp/GeoLite2-City.mmdb')
if os.path.isfile('/tmp/GeoLite2-ASN.mmdb'):
    reader_geo = geoip2.database.Reader('/tmp/GeoLite2-ASN.mmdb')


def get_geo_city(ip):
    try:
        response = reader_city.city(ip)
    except Exception:
        return None
    country_iso_code = response.country.iso_code
    country_name = response.country.name
    city_name = response.city.name
    __lon = response.location.longitude
    __lat = response.location.latitude
    location = {'lon': __lon, 'lat': __lat}
    return {'city_name': city_name, 'country_iso_code': country_iso_code,
            'country_name': country_name, 'location': location}


def get_geo_asn(ip):
    try:
        response = reader_geo.asn(ip)
    except Exception:
        return None
    return {'number': response.autonomous_system_number,
            'organization': {'name': response.autonomous_system_organization}}


def get_mime(data):
    if data.startswith(b'\x1f\x8b'):
        return 'gzip'
    elif data.startswith(b'\x50\x4b'):
        return 'zip'
    elif data.startswith(b'\x42\x5a'):
        return 'bzip2'
    textchars = bytearray(
        {7, 8, 9, 10, 12, 13, 27} | set(range(0x20, 0x100)) - {0x7f})
    if bool(data.translate(None, textchars)):
        return 'binary'
    else:
        return 'text'


def get_value_from_dict(dct, xkeys_list):
    """ 入れ子になった辞書に対して、dotを含んだkeyで値を
    抽出する。keyはリスト形式で複数含んでいたら分割する。
    値がなければ返値なし

    >>> dct = {'a': {'b': {'c': 123}}}
    >>> xkey = 'a.b.c'
    >>> get_value_from_dict(dct, xkey)
    123
    >>> xkey = 'x.y.z'
    >>> get_value_from_dict(dct, xkey)

    >>> xkeys_list = 'a.b.c x.y.z'
    >>> get_value_from_dict(dct, xkeys_list)
    123
    >>> dct = {'a': {'b': [{'c': 123}, {'c': 456}]}}
    >>> xkeys_list = 'a.b.0.c'
    >>> get_value_from_dict(dct, xkeys_list)
    123
    """
    for xkeys in xkeys_list.split():
        v = dct
        for k in xkeys.split('.'):
            try:
                k = int(k)
            except ValueError:
                pass
            try:
                v = v[k]
            except (TypeError, KeyError, IndexError):
                v = ''
                break
        if v:
            return v


def put_value_into_dict(key_str, v):
    """dictのkeyにドットが含まれている場合に入れ子になったdictを作成し、値としてvを入れる.
    返値はdictタイプ。vが辞書ならさらに入れ子として代入。
    TODO: 値に"が入ってると例外になる。対処方法が見つからず返値なDROPPEDにしてるので改善する。#34

    >>> put_value_into_dict('a.b.c', 123)
    {'a': {'b': {'c': '123'}}}
    >>> v = {'x': 1, 'y': 2}
    >>> put_value_into_dict('a.b.c', v)
    {'a': {'b': {'c': {'x': 1, 'y': 2}}}}
    >>> v = str({'x': "1", 'y': '2"3'})
    >>> put_value_into_dict('a.b.c', v)
    {'a': {'b': {'c': 'DROPPED'}}}
    """
    v = v
    xkeys = key_str.split('.')
    if isinstance(v, dict):
        json_data = r'{{"{0}": {1} }}'.format(xkeys[-1], json.dumps(v))
    else:
        json_data = r'{{"{0}": "{1}" }}'.format(xkeys[-1], v)
    if len(xkeys) >= 2:
        xkeys.pop()
        for xkey in reversed(xkeys):
            json_data = r'{{"{0}": {1} }}'.format(xkey, json_data)
    try:
        new_dict = json.loads(json_data, strict=False)
    except json.decoder.JSONDecodeError:
        new_dict = put_value_into_dict(key_str, 'DROPPED')
    return new_dict


def conv_key(obj):
    """dictのkeyに-が入ってたら_に置換する
    """
    if isinstance(obj, dict):
        for org_key in list(obj.keys()):
            new_key = org_key
            if '-' in org_key:
                new_key = org_key.translate({ord('-'): ord('_')})
                obj[new_key] = obj.pop(org_key)
            conv_key(obj[new_key])
    elif isinstance(obj, list):
        for val in obj:
            conv_key(val)
    else:
        pass


def merge(a, b, path=None):
    """merges b into a
    """
    if path is None:
        path = []
    for key in b:
        if key in a:
            if isinstance(a[key], dict) and isinstance(b[key], dict):
                merge(a[key], b[key], path + [str(key)])
            elif a[key] == b[key]:
                pass  # same leaf value
            elif str(a[key]) in str(b[key]):
                # strで上書き。JSONだったのをstrに変換したデータ
                a[key] = b[key]
            else:
                # conflict and override original value with new one
                a[key] = b[key]
        else:
            a[key] = b[key]
    return a


class LogObj:
    """ 取得した一連のログファイルから表層的な情報を取得する。
    圧縮の有無の判断、ログ種類を判断、フォーマットの判断をして
    最後に、生ファイルを個々のログに分割してリスト型として返す
    """
    def __init__(self, config):
        self.config = config
        self.s3bucket = None
        self.s3key = None
        self.loggroup = None
        self.logstream = None

    @property
    def header(self):
        return None


class LogS3(LogObj):
    """ 取得した一連のログファイルから表層的な情報を取得する。
    圧縮の有無の判断、ログ種類を判断、フォーマットの判断をして
    最後に、生ファイルを個々のログに分割してリスト型として返す
    """
    def __init__(self, record, config, s3):
        # Get the bucket name and key for the new file
        super().__init__(config)
        self.s3 = s3
        self.s3bucket = record['s3']['bucket']['name']
        self.s3key = record['s3']['object']['key']
        self.config = config
        self.ignore = self.check_ignore()
        self.msgformat = 's3'

    def check_ignore(self):
        if 'unknown' in self.logtype:
            # 対応していないlogtypeはunknownになる。その場合は処理をスキップさせる
            return f'Unknown log type in S3 key, {self.s3key}'
        else:
            s3_key_ignored = self.config[self.logtype]['s3_key_ignored']
            if s3_key_ignored and s3_key_ignored in self.s3key:
                return f'impossible to find logtype from S3 key, {self.s3key}'
        return False

    @property
    def logtype(self):
        for section in self.config.sections():
            p = self.config[section]['s3_key']
            if re.search(p, self.s3key):
                return section
        else:
            return 'unknown'

    @property
    def file_format(self):
        return self.config[self.logtype]['file_format']

    @property
    def accountid(self):
        m = RE_ACCOUNT.search(self.s3key)
        if m:
            return(m.group(1))
        else:
            return None

    @property
    def region(self):
        m = RE_REGION.search(self.s3key)
        if m:
            return(m.group(1))
        else:
            return None

    @property
    def rawdata(self):
        obj = self.s3.get_object(Bucket=self.s3bucket, Key=self.s3key)
        # if obj['ResponseMetadata']['HTTPHeaders']['content-length'] == '0':
        #    raise Exception('No Contents in s3 object')
        rawbody = io.BytesIO(obj['Body'].read())
        mime = get_mime(rawbody.read(16))
        rawbody.seek(0)
        if mime == 'gzip':
            body = gzip.open(rawbody, mode='rt', encoding='utf8',
                             errors='ignore')
        elif mime == 'text':
            body = io.TextIOWrapper(rawbody, encoding='utf8', errors='ignore')
        elif mime == 'zip':
            z = zipfile.ZipFile(rawbody)
            body = open(z.namelist()[0], encoding='utf8', errors='ignore')
        elif mime == 'bzip2':
            body = bz2.open(rawbody, mode='rt', encoding='utf8',
                            errors='ignore')
        else:
            raise Exception('unknown file format')
        return body

    @property
    def header(self):
        if 'csv' in self.file_format:
            return self.rawdata.readlines()[0].strip()
        else:
            return None

    @property
    def logdata_list(self):
        if 'text' in self.file_format:
            header_line_number = int(
                self.config[self.logtype]['text_header_line_number'])
            for logdata in self.rawdata.readlines()[header_line_number:]:
                yield logdata.strip()
        elif 'csv' in self.file_format:
            for logdata in self.rawdata.readlines()[1:]:
                yield logdata.strip()
        elif 'json' in self.file_format:
            decoder = json.JSONDecoder()
            # jsonl(1ファイルに1行のJSONが複数ある)を分割
            for line in self.rawdata.readlines():
                raw_event = decoder.decode(line)
                delimiter = self.config[self.logtype]['json_delimiter']
                if delimiter:
                    # 1つのJSONにログが複数ある場合
                    for record in raw_event[delimiter]:
                        yield record
                else:
                    yield raw_event

    @property
    def startmsg(self):
        startmsg = 's3 bucket: {0}, key: {1}, logtype: {2}'.format(
            self.s3bucket, self.s3key, self.logtype)
        return startmsg


class LogKinesis(LogObj):
    """ Kinesisで受信したCWLのログから表層的に情報を取得する。
    圧縮の有無の判断、ログ種類を判断、フォーマットの判断をして
    最後に、生ファイルを個々のログに分割してリスト型として返す
    入力値となるKinesisのJSONサンプルはこちら
    https://docs.aws.amazon.com/ja_jp/lambda/latest/dg/with-kinesis-example.html
    """
    def __init__(self, record, config):
        super().__init__(config)
        self.config = config
        self.rawdata_dict = self.get_rawdata_dict(record)
        self.loggroup = self.rawdata_dict['logGroup']
        self.logstream = self.rawdata_dict['logStream']
        self.msgformat = 'kinesis'
        self.ignore = self.check_ignore()
        self.__file_format = None

    def get_rawdata_dict(self, record):
        payload = base64.b64decode(record['kinesis']['data'])
        gzipbody = io.BytesIO(payload)
        body = gzip.open(gzipbody, mode='rt').readline()
        body_dict = json.loads(body)
        return body_dict

    def check_ignore(self):
        if 'CONTROL_MESSAGE' in self.rawdata_dict['messageType']:
            return "Kinesis's control_message"
        if 'unknown' in self.logtype:
            # 対応していないlogtypeはunknownになる。その場合は処理をスキップさせる
            return "Unknown log type in kinesis"
        else:
            return False

    @property
    def logtype(self):
        for section in self.config.sections():
            if self.config[section]['loggroup'] in self.loggroup.lower():
                return section
            else:
                try:
                    # CWEでログをCWLに送るとaws sourceが入ってるのでそれで評価
                    meta = self.rawdata_dict['logEvents'][0]['message'][:150]
                except KeyError:
                    meta = ''
                if self.config[section]['loggroup'] in meta.lower():
                    return section
        return 'unknown'

    @property
    def accountid(self):
        return self.rawdata_dict['owner']

    @property
    def region(self):
        text = self.loggroup.lower() + '_' + self.logstream.lower()
        m = RE_REGION.search(text)
        if m:
            return(m.group(1))
        else:
            return None

    @property
    def logdata_list(self):
        for record in self.rawdata_dict['logEvents']:
            # CWLでJSON化してる場合 eg) vpcflowlogs
            if 'extractedFields' in record:
                self.__file_format = 'json'
                yield record
                continue
            if self.config[self.logtype]['file_format'] == 'text':
                yield record['message']
                continue
            record = json.loads(record['message'])
            # CWEにて送られたCWLかどうかの判定 eg) securityhub, guardduty
            cwl_keys = ('source', 'detail', 'resources', 'account', 'time')
            if all(k in record for k in cwl_keys):
                record = record['detail']
                # 1つのJSNにログが複数ある場合 eg) securityhub
                delimiter = self.config[self.logtype]['json_delimiter']
                if delimiter:
                    for each_event in record[delimiter]:
                        yield each_event
                else:
                    yield record
            else:
                yield record

    @property
    def startmsg(self):
        startmsg = ('AccountID: {0}, logGroup: {1}, logStream: {2}'.format(
            self.accountid, self.loggroup, self.logstream))
        return startmsg

    @property
    def file_format(self):
        if self.__file_format:
            return self.__file_format
        else:
            return self.config[self.logtype]['file_format']


class LogParser:
    """ 生ファイルから、ファイルタイプ毎に、タイムスタンプの抜き出し、
    テキストなら名前付き正規化による抽出、エンリッチ(geoipなどの付与)、
    フィールドのECSへの統一、最後にJSON化、する
    """
    def __init__(self, logdata, logtype, logconfig, msgformat=None,
                 logformat=None, header=None, s3bucket=None, s3key=None,
                 loggroup=None, logstream=None, accountid=None, region=None,
                 log_pattern_prog=None, sf_module=None, *args, **kwargs):
        self.msgformat = msgformat
        self.logdata = logdata
        self.logtype = logtype
        self.logconfig = logconfig
        self.logformat = logformat
        self.s3bucket = s3bucket
        self.s3key = s3key
        self.loggroup = loggroup
        self.logstream = logstream
        self.accountid = accountid
        self.region = region
        self.log_pattern_prog = log_pattern_prog
        self.header = header
        self.__logdata_dict = self.logdata_to_dict()
        self.sf_module = sf_module

    def logdata_to_dict(self):
        logdata_dict = {}
        if 'kinesis' in self.msgformat and 'extractedFields' in self.logdata:
            # CWLでJSON化してる場合
            logdata_dict = self.logdata['extractedFields']
        elif self.logformat in 'csv':
            logdata_dict = dict(zip(self.header.split(), self.logdata.split()))
            conv_key(logdata_dict)
        elif self.logformat in 'json':
            logdata_dict = self.logdata
        elif self.logformat in 'text':
            try:
                m = self.log_pattern_prog.match(self.logdata)
            except AttributeError:
                raise AttributeError(
                    'You need to define log format and relevant configuration')
            if m:
                logdata_dict = m.groupdict()
            else:
                raise Exception(
                    f'Invalid regex pattern of {self.logtype} in aws.ini or '
                    f'use.ini.\nregex_pattern:\n{self.log_pattern_prog}\n'
                    f'rawdata:\n{self.logdata}\n')

        return logdata_dict

    def check_ignored_log(self, ignore_list):
        if self.logtype in ignore_list:
            for key in ignore_list[self.logtype]:
                if key in self.__logdata_dict:
                    value = self.__logdata_dict[key]
                    if value and ignore_list[self.logtype][key] in value:
                        return True
        return False

    def add_basic_field(self):
        basic_dict = {}
        if 'kinesis' in self.msgformat and 'extractedFields' in self.logdata:
            basic_dict['@message'] = self.logdata['message']
        elif self.logformat in 'json':
            basic_dict['@message'] = str(json.dumps(self.logdata))
        else:
            basic_dict['@message'] = str(self.logdata)
        basic_dict['event'] = {'module': self.logtype}
        self.__timestamp = self.get_timestamp()
        basic_dict['@timestamp'] = self.timestamp.isoformat()
        self.__event_ingested = datetime.now(timezone.utc)
        basic_dict['event']['ingested'] = self.event_ingested.isoformat()
        basic_dict['@log_type'] = self.logtype
        if self.logconfig['doc_id']:
            basic_dict['@id'] = self.__logdata_dict[self.logconfig['doc_id']]
        else:
            basic_dict['@id'] = hashlib.md5(
                str(basic_dict['@message']).encode('utf-8')).hexdigest()
        if self.loggroup:
            basic_dict['@log_group'] = self.loggroup
            basic_dict['@log_stream'] = self.logstream
        if self.s3bucket:
            basic_dict['@log_s3bucket'] = self.s3bucket
            basic_dict['@log_s3key'] = self.s3key
        self.__logdata_dict.update(basic_dict)

    def clean_multi_type_field(self):
        clean_multi_type_dict = {}
        multifield_keys = self.logconfig['json_to_text'].split()
        for multifield_key in multifield_keys:
            v = get_value_from_dict(self.__logdata_dict, multifield_key)
            if v:
                # json obj in json obj
                if isinstance(v, int):
                    new_dict = put_value_into_dict(multifield_key, v)
                elif '{' in v:
                    new_dict = put_value_into_dict(multifield_key, repr(v))
                else:
                    new_dict = put_value_into_dict(multifield_key, str(v))
                merge(clean_multi_type_dict, new_dict)
        merge(self.__logdata_dict, clean_multi_type_dict)

    def transform_to_ecs(self):
        ecs_dict = {'ecs': {'version': self.logconfig['ecs_version']}}
        if self.logconfig['cloud_provider']:
            ecs_dict['cloud'] = {'provider': self.logconfig['cloud_provider']}
        ecs_keys = self.logconfig['ecs'].split()
        for ecs_key in ecs_keys:
            original_keys = self.logconfig[ecs_key]
            v = get_value_from_dict(self.__logdata_dict, original_keys)
            if v:
                # disable after ecs1.6.0
                # 特定のECSは全部小文字にする
                # lower_keys = ('http.request.method')
                # if ecs_key in lower_keys:
                #    v = v.lower()
                new_ecs_dict = put_value_into_dict(ecs_key, v)
                if '.ip' in ecs_key:
                    # IPアドレスの場合は、validation
                    try:
                        ipaddress.ip_address(v)
                    except ValueError:
                        continue
                merge(ecs_dict, new_ecs_dict)
        if 'cloud' in ecs_dict:
            if 'account' in ecs_dict['cloud'] \
                    and 'id' in ecs_dict['cloud']['account']:
                pass
            elif self.accountid:
                ecs_dict['cloud']['account'] = {'id': self.accountid}
            if 'region' in ecs_dict['cloud']:
                pass
            elif self.region:
                ecs_dict['cloud']['region'] = self.region
            else:
                ecs_dict['cloud']['region'] = 'unknown'
        static_ecs_keys = self.logconfig.get('static_ecs')
        if static_ecs_keys:
            for static_ecs_key in static_ecs_keys.split():
                new_ecs_dict = put_value_into_dict(
                    static_ecs_key, self.logconfig[static_ecs_key])
                merge(ecs_dict, new_ecs_dict)
        merge(self.__logdata_dict, ecs_dict)

    def transform_by_script(self):
        if self.logconfig['script_ecs']:
            self.__logdata_dict = self.sf_module.transform(self.__logdata_dict)

    def enrich(self):
        enrich_dict = {}
        # geoip
        if not reader_city:
            return None
        geoip_list = self.logconfig['geoip'].split()
        for geoip_ecs in geoip_list:
            try:
                ipaddr = self.__logdata_dict[geoip_ecs]['ip']
            except KeyError:
                continue
            geoip = get_geo_city(ipaddr)
            if geoip:
                enrich_dict[geoip_ecs] = {'geo': geoip}
            asn = get_geo_asn(ipaddr)
            if geoip and asn:
                enrich_dict[geoip_ecs].update({'as': asn})
            elif asn:
                enrich_dict[geoip_ecs] = {'as': asn}
        merge(self.__logdata_dict, enrich_dict)

    @property
    def index_id(self):
        if self.logconfig['doc_id_suffix']:
            suffix = get_value_from_dict(
                self.__logdata_dict, self.logconfig.get('doc_id_suffix', 0))
            return '{0}_{1}'.format(self.__logdata_dict['@id'], suffix)
        else:
            return self.__logdata_dict['@id']

    def get_timestamp(self):
        if 'timestamp' in self.logconfig and self.logconfig['timestamp']:
            # this is depprecatd code of v1.5.2 and keep for compatibility
            timestamp_list = self.logconfig['timestamp'].split(',')
            self.logconfig['timestamp_key'] = timestamp_list[0]
            if len(timestamp_list) == 2:
                self.logconfig['timestamp_format'] = timestamp_list[1]
            # フォーマットの指定がなければISO9601と仮定。
        if self.logconfig['timestamp_key']:
            # new code from ver 1.6.0
            timestamp_key = self.logconfig['timestamp_key']
            timestamp_format = self.logconfig['timestamp_format']
            timestamp_tz = float(self.logconfig['timestamp_tz'])
            TZ = timezone(timedelta(hours=timestamp_tz))
            # 末尾がZはPythonでは対応していないのでカットしてTZを付与
            try:
                timestr = self.__logdata_dict[timestamp_key].replace(
                    'Z', '+00:00')
            except AttributeError:
                # int such as epoch
                timestr = self.__logdata_dict[timestamp_key]
            if 'epoch' in timestamp_format:
                epoch = float(timestr)
                if epoch > 1000000000000:
                    # milli epoch
                    dt = datetime.fromtimestamp(epoch/1000, tz=TZ)
                else:
                    # normal epoch
                    dt = datetime.fromtimestamp(epoch, tz=TZ)
            elif 'syslog' in timestamp_format:
                # timezoneを考慮して、12時間を早めた現在時刻を基準とする
                now = datetime.now(timezone.utc) + TD_OFFSET12
                m = RE_SYSLOG_FORMAT.match(timestr)
                try:
                    # コンマ以下の秒があったら
                    microsec = int(m.group(7).ljust(6, '0'))
                except AttributeError:
                    microsec = 0
                try:
                    dt = datetime(
                        year=now.year, month=MONTH_TO_INT[m.group(1)],
                        day=int(m.group(2)), hour=int(m.group(3)),
                        minute=int(m.group(4)), second=int(m.group(5)),
                        microsecond=microsec, tzinfo=TZ)
                except ValueError:
                    # うるう年対策
                    dt = datetime(
                        year=now.year-1, month=MONTH_TO_INT[m.group(1)],
                        day=int(m.group(2)), hour=int(m.group(3)),
                        minute=int(m.group(4)), second=int(m.group(5)),
                        microsecond=microsec, tzinfo=TZ)
                if dt > now:
                    # syslog timestamp が未来。マイナス1年の補正が必要
                    # 1年以上古いログの補正はできない
                    dt = dt.replace(year=now.year-1)
                else:
                    # syslog timestamp が過去であり適切。処理なし
                    pass
            elif 'iso8601' in timestamp_format:
                try:
                    dt = datetime.fromisoformat(timestr)
                except ValueError as err:
                    raise ValueError(
                        'ERROR: timestamp {0} is not ISO9601. See details {1}'
                        ''.format(self.logconfig['timestamp_key'], err))
                if not dt.tzinfo:
                    dt = dt.replace(tzinfo=TZ)
            elif timestamp_format:
                try:
                    dt = datetime.strptime(timestr, timestamp_format)
                except ValueError as err:
                    raise ValueError(
                        'ERROR: timestamp key {0} is wrong. See details {1}'
                        ''.format(self.logconfig['timestamp_key'], err))
                if not dt.tzinfo:
                    dt = dt.replace(tzinfo=TZ)
            else:
                raise ValueError(
                    "ERROR: There is no timestamp format. It's necessary")
        else:
            dt = datetime.now(timezone.utc)
        return dt

    @property
    def timestamp(self):
        return self.__timestamp

    @property
    def event_ingested(self):
        return self.__event_ingested

    @property
    def indexname(self):
        indexname = self.logconfig['index_name']
        if 'auto' in self.logconfig['index_rotation']:
            return indexname
        if 'event_ingested' in self.logconfig['index_time']:
            index_dt = self.event_ingested
        else:
            index_dt = self.timestamp
        if self.logconfig['index_tz']:
            TZ = timezone(timedelta(hours=float(self.logconfig['index_tz'])))
            index_dt = index_dt.astimezone(TZ)
        if 'daily' in self.logconfig['index_rotation']:
            return indexname + index_dt.strftime('-%Y-%m-%d')
        elif 'weekly' in self.logconfig['index_rotation']:
            return indexname + index_dt.strftime('-%Y-w%W')
        elif 'monthly' in self.logconfig['index_rotation']:
            return indexname + index_dt.strftime('-%Y-%m')
        else:
            return indexname + index_dt.strftime('-%Y')

    def del_none(self, d):
        """ 値のないキーを削除する。削除しないとESへのLoad時にエラーとなる """
        for key, value in list(d.items()):
            if isinstance(value, dict) and len(value) == 0:
                del d[key]
            elif isinstance(value, str) and (value in ('', '-', 'null')):
                del d[key]
            elif isinstance(value, dict):
                self.del_none(value)
        return d

    @property
    def json(self):
        self.__logdata_dict = self.del_none(self.del_none(self.__logdata_dict))
        return json.dumps(self.__logdata_dict)
