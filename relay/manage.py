import Crypto
import asyncio
import click
import logging
import platform

from urllib.parse import urlparse

from . import misc, __version__
from .application import Application
from .config import relay_software_names


app = None


@click.group('cli', context_settings={'show_default': True}, invoke_without_command=True)
@click.option('--config', '-c', default='relay.yaml', help='path to the relay\'s config')
@click.version_option(version=__version__, prog_name='ActivityRelay')
@click.pass_context
def cli(ctx, config):
	global app
	app = Application(config)

	if not ctx.invoked_subcommand:
		if app.config.host.endswith('example.com'):
			relay_setup.callback()

		else:
			relay_run.callback()


@cli.group('inbox')
@click.pass_context
def cli_inbox(ctx):
	'Manage the inboxes in the database'
	pass


@cli_inbox.command('list')
def cli_inbox_list():
	'List the connected instances or relays'

	click.echo('Connected to the following instances or relays:')

	for inbox in app.database.inboxes:
		click.echo(f'- {inbox}')


@cli_inbox.command('follow')
@click.argument('actor')
def cli_inbox_follow(actor):
	'Follow an actor (Relay must be running)'

	if app.config.is_banned(actor):
		return click.echo(f'Error: Refusing to follow banned actor: {actor}')

	if not actor.startswith('http'):
		domain = actor
		actor = f'https://{actor}/actor'

	else:
		domain = urlparse(actor).hostname

	try:
		inbox_data = app.database['relay-list'][domain]
		inbox = inbox_data['inbox']

	except KeyError:
		actor_data = asyncio.run(misc.request(actor))
		inbox = actor_data.shared_inbox

	message = misc.Message.new_follow(
		host = app.config.host,
		actor = actor.id
	)

	asyncio.run(misc.request(inbox, message))
	click.echo(f'Sent follow message to actor: {actor}')


@cli_inbox.command('unfollow')
@click.argument('actor')
def cli_inbox_unfollow(actor):
	'Unfollow an actor (Relay must be running)'

	if not actor.startswith('http'):
		domain = actor
		actor = f'https://{actor}/actor'

	else:
		domain = urlparse(actor).hostname

	try:
		inbox_data = app.database['relay-list'][domain]
		inbox = inbox_data['inbox']
		message = misc.Message.new_unfollow(
			host = app.config.host,
			actor = actor,
			follow = inbox_data['followid']
		)

	except KeyError:
		actor_data = asyncio.run(misc.request(actor))
		inbox = actor_data.shared_inbox
		message = misc.Message.new_unfollow(
			host = app.config.host,
			actor = actor,
			follow = {
				'type': 'Follow',
				'object': actor,
				'actor': f'https://{app.config.host}/actor'
			}
		)

	asyncio.run(misc.request(inbox, message))
	click.echo(f'Sent unfollow message to: {actor}')


@cli_inbox.command('add')
@click.argument('inbox')
def cli_inbox_add(inbox):
	'Add an inbox to the database'

	if not inbox.startswith('http'):
		inbox = f'https://{inbox}/inbox'

	if app.config.is_banned(inbox):
		return click.echo(f'Error: Refusing to add banned inbox: {inbox}')

	if app.database.get_inbox(inbox):
		return click.echo(f'Error: Inbox already in database: {inbox}')

	app.database.add_inbox(inbox)
	app.database.save()

	click.echo(f'Added inbox to the database: {inbox}')


@cli_inbox.command('remove')
@click.argument('inbox')
def cli_inbox_remove(inbox):
	'Remove an inbox from the database'

	try:
		dbinbox = app.database.get_inbox(inbox, fail=True)

	except KeyError:
		click.echo(f'Error: Inbox does not exist: {inbox}')
		return

	app.database.del_inbox(dbinbox['domain'])
	app.database.save()

	click.echo(f'Removed inbox from the database: {inbox}')


@cli.group('instance')
def cli_instance():
	'Manage instance bans'
	pass


@cli_instance.command('list')
def cli_instance_list():
	'List all banned instances'

	click.echo('Banned instances or relays:')

	for domain in app.config.blocked_instances:
		click.echo(f'- {domain}')


@cli_instance.command('ban')
@click.argument('target')
def cli_instance_ban(target):
	'Ban an instance and remove the associated inbox if it exists'

	if target.startswith('http'):
		target = urlparse(target).hostname

	if app.config.ban_instance(target):
		app.config.save()

		if app.database.del_inbox(target):
			app.database.save()

		click.echo(f'Banned instance: {target}')
		return

	click.echo(f'Instance already banned: {target}')


@cli_instance.command('unban')
@click.argument('target')
def cli_instance_unban(target):
	'Unban an instance'

	if app.config.unban_instance(target):
		app.config.save()

		click.echo(f'Unbanned instance: {target}')
		return

	click.echo(f'Instance wasn\'t banned: {target}')


@cli.group('software')
def cli_software():
	'Manage banned software'
	pass


@cli_software.command('list')
def cli_software_list():
	'List all banned software'

	click.echo('Banned software:')

	for software in app.config.blocked_software:
		click.echo(f'- {software}')


@cli_software.command('ban')
@click.option('--fetch-nodeinfo/--ignore-nodeinfo', '-f', 'fetch_nodeinfo', default=False,
	help='Treat NAME like a domain and try to fet the software name from nodeinfo'
)
@click.argument('name')
def cli_software_ban(name, fetch_nodeinfo):
	'Ban software. Use RELAYS for NAME to ban relays'

	if name == 'RELAYS':
		for name in relay_software_names:
			app.config.ban_software(name)

		app.config.save()
		return click.echo('Banned all relay software')

	if fetch_nodeinfo:
		nodeinfo = asyncio.run(misc.fetch_nodeinfo(name))

		if not software:
			click.echo(f'Failed to fetch software name from domain: {name}')

	if config.ban_software(nodeinfo.swname):
		app.config.save()
		return click.echo(f'Banned software: {nodeinfo.swname}')

	click.echo(f'Software already banned: {nodeinfo.swname}')


@cli_software.command('unban')
@click.option('--fetch-nodeinfo/--ignore-nodeinfo', '-f', 'fetch_nodeinfo', default=False,
	help='Treat NAME like a domain and try to fet the software name from nodeinfo'
)
@click.argument('name')
def cli_software_unban(name, fetch_nodeinfo):
	'Ban software. Use RELAYS for NAME to unban relays'

	if name == 'RELAYS':
		for name in relay_software_names:
			app.config.unban_software(name)

		config.save()
		return click.echo('Unbanned all relay software')

	if fetch_nodeinfo:
		nodeinfo = asyncio.run(misc.fetch_nodeinfo(name))

		if not nodeinfo:
			click.echo(f'Failed to fetch software name from domain: {name}')

	if app.config.unban_software(nodeinfo.swname):
		app.config.save()
		return click.echo(f'Unbanned software: {nodeinfo.swname}')

	click.echo(f'Software wasn\'t banned: {nodeinfo.swname}')



@cli.group('whitelist')
def cli_whitelist():
	'Manage the instance whitelist'
	pass


@cli_whitelist.command('list')
def cli_whitelist_list():
	click.echo('Current whitelisted domains')

	for domain in app.config.whitelist:
		click.echo(f'- {domain}')


@cli_whitelist.command('add')
@click.argument('instance')
def cli_whitelist_add(instance):
	'Add an instance to the whitelist'

	if not app.config.add_whitelist(instance):
		return click.echo(f'Instance already in the whitelist: {instance}')

	app.config.save()
	click.echo(f'Instance added to the whitelist: {instance}')


@cli_whitelist.command('remove')
@click.argument('instance')
def cli_whitelist_remove(instance):
	'Remove an instance from the whitelist'

	if not app.config.del_whitelist(instance):
		return click.echo(f'Instance not in the whitelist: {instance}')

	app.config.save()

	if app.config.whitelist_enabled:
		if app.database.del_inbox(inbox):
			app.database.save()

	click.echo(f'Removed instance from the whitelist: {instance}')


@cli.command('setup')
def relay_setup():
	'Generate a new config'

	while True:
		app.config.host = click.prompt('What domain will the relay be hosted on?', default=app.config.host)

		if not config.host.endswith('example.com'):
			break

		click.echo('The domain must not be example.com')

	app.config.listen = click.prompt('Which address should the relay listen on?', default=app.config.listen)

	while True:
		app.config.port = click.prompt('What TCP port should the relay listen on?', default=app.config.port, type=int)
		break

	app.config.save()

	if not app['is_docker'] and click.confirm('Relay all setup! Would you like to run it now?'):
		relay_run.callback()


@cli.command('run')
def relay_run():
	'Run the relay'

	if app.config.host.endswith('example.com'):
		return click.echo('Relay is not set up. Please edit your relay config or run "activityrelay setup".')

	vers_split = platform.python_version().split('.')
	pip_command = 'pip3 uninstall pycrypto && pip3 install pycryptodome'

	if Crypto.__version__ == '2.6.1':
		if int(vers_split[1]) > 7:
			click.echo('Error: PyCrypto is broken on Python 3.8+. Please replace it with pycryptodome before running again. Exiting...')
			return click.echo(pip_command)

		else:
			click.echo('Warning: PyCrypto is old and should be replaced with pycryptodome')
			return click.echo(pip_command)

	if not misc.check_open_port(app.config.listen, app.config.port):
		return click.echo(f'Error: A server is already running on port {app.config.port}')

	app.run()


def main():
	cli(prog_name='relay')


if __name__ == '__main__':
	click.echo('Running relay.manage is depreciated. Run `activityrelay [command]` instead.')
