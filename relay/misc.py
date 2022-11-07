import asyncio
import base64
import json
import logging
import socket
import traceback
import uuid

from Crypto.Hash import SHA, SHA256, SHA512
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from aiohttp import ClientSession
from datetime import datetime
from json.decoder import JSONDecodeError
from urllib.parse import urlparse
from uuid import uuid4

from .http_debug import http_debug


app = None
HASHES = {
	'sha1': SHA,
	'sha256': SHA256,
	'sha512': SHA512
}


def set_app(new_app):
	global app
	app = new_app


def build_signing_string(headers, used_headers):
	return '\n'.join(map(lambda x: ': '.join([x.lower(), headers[x]]), used_headers))


def check_open_port(host, port):
	if host == '0.0.0.0':
		host = '127.0.0.1'

	with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
		try:
			return s.connect_ex((host , port)) != 0

		except socket.error as e:
			return False


def create_signature_header(headers):
	headers = {k.lower(): v for k, v in headers.items()}
	used_headers = headers.keys()
	sigstring = build_signing_string(headers, used_headers)

	sig = {
		'keyId': app['config'].keyid,
		'algorithm': 'rsa-sha256',
		'headers': ' '.join(used_headers),
		'signature': sign_signing_string(sigstring, app['database'].PRIVKEY)
	}

	chunks = ['{}="{}"'.format(k, v) for k, v in sig.items()]
	return ','.join(chunks)


def distill_inboxes(actor, object_id):
	database = app['database']

	for inbox in database.inboxes:
		if inbox != actor.shared_inbox and urlparse(inbox).hostname != urlparse(object_id).hostname:
			yield inbox


def generate_body_digest(body):
	bodyhash = app['cache'].digests.get(body)

	if bodyhash:
		return bodyhash

	h = SHA256.new(body.encode('utf-8'))
	bodyhash = base64.b64encode(h.digest()).decode('utf-8')
	app['cache'].digests[body] = bodyhash

	return bodyhash


def sign_signing_string(sigstring, key):
	pkcs = PKCS1_v1_5.new(key)
	h = SHA256.new()
	h.update(sigstring.encode('ascii'))
	sigdata = pkcs.sign(h)

	return base64.b64encode(sigdata).decode('utf-8')


def split_signature(sig):
	default = {"headers": "date"}

	sig = sig.strip().split(',')

	for chunk in sig:
		k, _, v = chunk.partition('=')
		v = v.strip('\"')
		default[k] = v

	default['headers'] = default['headers'].split()
	return default


async def fetch_actor_key(actor):
	actor_data = await request(actor)

	if not actor_data:
		return None

	try:
		return RSA.importKey(actor_data['publicKey']['publicKeyPem'])

	except Exception as e:
		logging.debug(f'Exception occured while fetching actor key: {e}')


async def fetch_nodeinfo(domain):
	nodeinfo_url = None

	wk_nodeinfo = await request(f'https://{domain}/.well-known/nodeinfo', sign_headers=False, activity=False)

	if not wk_nodeinfo:
		return

	for link in wk_nodeinfo.get('links', ''):
		if link['rel'] == 'http://nodeinfo.diaspora.software/ns/schema/2.0':
			nodeinfo_url = link['href']
			break

	if not nodeinfo_url:
		return

	nodeinfo_data = await request(nodeinfo_url, sign_headers=False, activity=False)

	try:
		return nodeinfo_data['software']['name']

	except KeyError:
		return False


## todo: remove follow_remote_actor and unfollow_remote_actor
async def follow_remote_actor(actor_uri):
	config = app['config']

	actor = await request(actor_uri)

	if not actor:
		logging.error(f'failed to fetch actor at: {actor_uri}')
		return

	message = {
		"@context": "https://www.w3.org/ns/activitystreams",
		"type": "Follow",
		"to": [actor['id']],
		"object": actor['id'],
		"id": f"https://{config.host}/activities/{uuid4()}",
		"actor": f"https://{config.host}/actor"
	}

	logging.verbose(f'sending follow request: {actor_uri}')
	await request(actor.shared_inbox, message)


async def unfollow_remote_actor(actor_uri):
	config = app['config']

	actor = await request(actor_uri)

	if not actor:
		logging.error(f'failed to fetch actor: {actor_uri}')
		return

	message = {
		"@context": "https://www.w3.org/ns/activitystreams",
		"type": "Undo",
		"to": [actor_uri],
		"object": {
			"type": "Follow",
			"object": actor_uri,
			"actor": actor_uri,
			"id": f"https://{config.host}/activities/{uuid4()}"
		},
		"id": f"https://{config.host}/activities/{uuid4()}",
		"actor": f"https://{config.host}/actor"
	}

	logging.verbose(f'sending unfollow request to inbox: {actor.shared_inbox}')
	await request(actor.shared_inbox, message)


async def request(uri, data=None, force=False, sign_headers=True, activity=True):
	## If a get request and not force, try to use the cache first
	if not data and not force:
		try:
			return app['cache'].json[uri]

		except KeyError:
			pass

	url = urlparse(uri)
	method = 'POST' if data else 'GET'
	action = data.get('type') if data else None
	headers = {
		'Accept': 'application/activity+json, application/json;q=0.9',
		'User-Agent': 'ActivityRelay',
	}

	if data:
		headers['Content-Type'] = 'application/activity+json' if activity else 'application/json'

	if sign_headers:
		signing_headers = {
			'(request-target)': f'{method.lower()} {url.path}',
			'Date': datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT'),
			'Host': url.netloc
		}

		if data:
			assert isinstance(data, dict)

			data = json.dumps(data)
			signing_headers.update({
				'Digest': f'SHA-256={generate_body_digest(data)}',
				'Content-Length': str(len(data.encode('utf-8')))
			})

		signing_headers['Signature'] = create_signature_header(signing_headers)

		del signing_headers['(request-target)']
		del signing_headers['Host']

		headers.update(signing_headers)

	try:
		if data:
			logging.verbose(f'Sending "{action}" to inbox: {uri}')

		else:
			logging.verbose(f'Sending GET request to url: {uri}')

		async with ClientSession(trace_configs=http_debug()) as session, app['semaphore']:
			async with session.request(method, uri, headers=headers, data=data) as resp:
				## aiohttp has been known to leak if the response hasn't been read,
				## so we're just gonna read the request no matter what
				resp_data = await resp.read()

				## Not expecting a response, so just return
				if resp.status == 202:
					return

				elif resp.status != 200:
					if not resp_data:
						return logging.verbose(f'Received error when requesting {uri}: {resp.status} {resp_data}')

					return logging.verbose(f'Received error when sending {action} to {uri}: {resp.status} {resp_data}')

				if resp.content_type == 'application/activity+json':
					resp_data = await resp.json(loads=Message.new_from_json)

				elif resp.content_type == 'application/json':
					resp_data = await resp.json(loads=DotDict.new_from_json)

				else:
					logging.verbose(f'Invalid Content-Type for "{url}": {resp.content_type}')
					return logging.debug(f'Response: {resp_data}')

				logging.debug(f'{uri} >> resp {resp_data}')

				app['cache'].json[uri] = resp_data
				return resp_data

	except JSONDecodeError:
		return

	except Exception:
		traceback.print_exc()


async def validate_signature(actor, http_request):
	pubkey = await fetch_actor_key(actor)

	if not pubkey:
		return False

	logging.debug(f'actor key: {pubkey}')

	headers = {key.lower(): value for key, value in http_request.headers.items()}
	headers['(request-target)'] = ' '.join([http_request.method.lower(), http_request.path])

	sig = split_signature(headers['signature'])
	logging.debug(f'sigdata: {sig}')

	sigstring = build_signing_string(headers, sig['headers'])
	logging.debug(f'sigstring: {sigstring}')

	sign_alg, _, hash_alg = sig['algorithm'].partition('-')
	logging.debug(f'sign alg: {sign_alg}, hash alg: {hash_alg}')

	sigdata = base64.b64decode(sig['signature'])

	pkcs = PKCS1_v1_5.new(pubkey)
	h = HASHES[hash_alg].new()
	h.update(sigstring.encode('ascii'))
	result = pkcs.verify(h, sigdata)

	http_request['validated'] = result

	logging.debug(f'validates? {result}')
	return result


class DotDict(dict):
	def __getattr__(self, k):
		try:
			return self[k]

		except KeyError:
			raise AttributeError(f'{self.__class__.__name__} object has no attribute {k}') from None


	def __setattr__(self, k, v):
		if k.startswith('_'):
			super().__setattr__(k, v)

		else:
			self[k] = v


	def __setitem__(self, k, v):
		if type(v) == dict:
			v = DotDict(v)

		super().__setitem__(k, v)


	def __delattr__(self, k):
		try:
			dict.__delitem__(self, k)

		except KeyError:
			raise AttributeError(f'{self.__class__.__name__} object has no attribute {k}') from None


	@classmethod
	def new_from_json(cls, data):
		if not data:
			raise JSONDecodeError('Empty body', data, 1)

		try:
			return cls(json.loads(data))

		except ValueError:
			raise JSONDecodeError('Invalid body', data, 1)


	def to_json(self, indent=None):
		return json.dumps(self, indent=indent)


class Message(DotDict):
	@classmethod
	def new_actor(cls, host, pubkey, description=None):
		return cls({
			'@context': 'https://www.w3.org/ns/activitystreams',
			'id': f'https://{host}/actor',
			'type': 'Application',
			'preferredUsername': 'relay',
			'name': 'ActivityRelay',
			'summary': description or 'ActivityRelay bot',
			'followers': f'https://{host}/followers',
			'following': f'https://{host}/following',
			'inbox': f'https://{host}/inbox',
			'url': f'https://{host}/inbox',
			'endpoints': {
				'sharedInbox': f'https://{host}/inbox'
			},
			'publicKey': {
				'id': f'https://{host}/actor#main-key',
				'owner': f'https://{host}/actor',
				'publicKeyPem': pubkey
			}
		})


	@classmethod
	def new_announce(cls, host, object):
		return cls({
			'@context': 'https://www.w3.org/ns/activitystreams',
			'id': f'https://{host}/activities/{uuid.uuid4()}',
			'type': 'Announce',
			'to': [f'https://{host}/followers'],
			'actor': f'https://{host}/actor',
			'object': object
		})


	@classmethod
	def new_follow(cls, host, actor):
		return cls({
			'@context': 'https://www.w3.org/ns/activitystreams',
			'type': 'Follow',
			'to': [actor],
			'object': actor,
			'id': f'https://{host}/activities/{uuid.uuid4()}',
			'actor': f'https://{host}/actor'
		})


	@classmethod
	def new_unfollow(cls, host, actor, follow):
		return cls({
			'@context': 'https://www.w3.org/ns/activitystreams',
			'id': f'https://{host}/activities/{uuid.uuid4()}',
			'type': 'Undo',
			'to': [actor],
			'actor': f'https://{host}/actor',
			'object': follow
		})


	@classmethod
	def new_response(cls, host, actor, followid, accept):
		return cls({
			'@context': 'https://www.w3.org/ns/activitystreams',
			'id': f'https://{host}/activities/{uuid.uuid4()}',
			'type': 'Accept' if accept else 'Reject',
			'to': [actor],
			'actor': f'https://{host}/actor',
			'object': {
				'id': followid,
				'type': 'Follow',
				'object': f'https://{host}/actor',
				'actor': actor
			}
		})


	# misc properties
	@property
	def domain(self):
		return urlparse(self.id).hostname


	# actor properties
	@property
	def pubkey(self):
		return self.publicKey.publicKeyPem


	@property
	def shared_inbox(self):
		return self.get('endpoints', {}).get('sharedInbox', self.inbox)


	# activity properties
	@property
	def actorid(self):
		if isinstance(self.actor, dict):
			return self.actor.id

		return self.actor


	@property
	def objectid(self):
		if isinstance(self.object, dict):
			return self.object.id

		return self.object
