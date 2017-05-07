from collections import defaultdict
import logging
import requests
from xml.etree import cElementTree as ET
from odoo import api, models, fields, _
from odoo.exceptions import UserError, Warning, ValidationError
from pyajam import Pyajam


_logger = logging.getLogger(__name__)


def etree_to_dict(t):
    """
    Helper function to parse Asterisk mxml responses over AJAM.
    """
    d = {t.tag: {} if t.attrib else None}
    children = list(t)
    if children:
        dd = defaultdict(list)
        for dc in map(etree_to_dict, children):
            for k, v in dc.items():
                dd[k].append(v)
        d = {t.tag: {k:v[0] if len(v) == 1 else v for k, v in dd.items()}}
    if t.attrib:
        d[t.tag].update(('@' + k, v) for k, v in t.attrib.items())
    if t.text:
        text = t.text.strip()
        if children or t.attrib:
            if text:
              d[t.tag]['#text'] = text
        else:
            d[t.tag] = text
    return d



class AsteriskServer(models.Model):
    _name = 'asterisk.server'

    name = fields.Char(required=True)
    host = fields.Char(required=True)
    note = fields.Text()
    ami_username = fields.Char(required=True, string='AMI username')
    ami_password = fields.Char(required=True, string='AMI password')
    ami_port = fields.Integer(required=True, default=5038, string='AMI port')
    http_port = fields.Integer(required=True, default=8088, string='HTTP port')
    https_port = fields.Integer(required=True, default=8089, string='HTTPS port')
    use_https = fields.Boolean(string='Use HTTPS')
    certificate = fields.Text(string='TLS certificate')
    key = fields.Text(string='TLS private key')
    conf_files = fields.One2many(comodel_name='asterisk.conf',
                                 inverse_name='server')
    sync_date = fields.Datetime(readonly=True)
    sync_uid = fields.Many2one('res.users', readonly=True, string='Sync by')


    def no_asterisk_mode(self):
        # Check Asterisk fake mode
        return self.env['ir.config_parameter'].get_param(
            'asterisk_base.no_asterisk', False)


    def asterisk_command(self, command):
        self.ensure_one()
        if self.no_asterisk_mode():
            return
        ajam = Pyajam(server=self.host,
                    username=self.ami_username,
                    password=self.ami_password,
                    port=self.http_port)
        if not ajam.login():
            raise UserError('Asterisk AMI login error!')
        response = ajam.command(command)



    def asterisk_reload(self):
        self.ensure_one()
        self.asterisk_command('reload')


    def sync_conf(self):
        self.ensure_one()
        if self.no_asterisk_mode():
            return
        # Start AJAM session
        s = requests.session()
        try:
            url = 'http://{}:{}/asterisk/mxml?action=Login&' \
                  'username={}&secret={}'.format(
                        self.host, self.http_port,
                        self.ami_username, self.ami_password)
            resp = s.get(url)
        except requests.ConnectionError as e:
            raise UserError('Cannot connect to Asterisk server!')
        # Check status
        if resp.status_code != 200:
            raise UserError('Asterisk server response status {}'.format(
                                                            resp.status_code))

        # Convert response from XML to {}
        mxml_response = ET.XML(resp.text)
        dict_response = etree_to_dict(mxml_response)
        if not dict_response.get('ajax-response'):
            _logger.error('AJAX response not found: {}'.format(mxml_response))
            raise Warning('AJAX response not found!')
        response = dict_response.get('ajax-response').get('response', {}).get(
            'generic', {}).get('@response')
        if response == 'Error':
            message = dict_response.get('ajax-response').get('response', {}).get(
                'generic', {}).get('@message')
            raise Warning('Error: {}!'.format(message))

        # Start sending config files to the server
        for conf in self.conf_files:
            url = 'http://{}:{}/asterisk/uploads'.format(
                                                    self.host, self.http_port)
            response = s.post(url,
                files={'file': (conf.filename, conf.content, 'text/plain', {'Content-type': 'text/plain'})})
            if 'File successfully uploaded' not in response.text:
                raise Warning('File upload error: {}.'.format(response.text))

        # Update last sync
        self.sync_date = fields.Datetime.now()
        self.sync_uid = self.env.uid

        # Finally reload Asterisk
        self.asterisk_reload()