#   Licensed under the Apache License, Version 2.0 (the "License"); you may
#   not use this file except in compliance with the License. You may obtain
#   a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#   WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#   License for the specific language governing permissions and limitations
#   under the License.
#

"""Orchestration v1 Stack action implementations"""

import logging
import sys

from cliff import command
from cliff import lister
from cliff import show
from openstackclient.common import exceptions as exc
from openstackclient.common import parseractions
from openstackclient.common import utils
from oslo_serialization import jsonutils
import six
from six.moves.urllib import request

from heatclient.common import event_utils
from heatclient.common import format_utils
from heatclient.common import hook_utils
from heatclient.common import http
from heatclient.common import template_utils
from heatclient.common import utils as heat_utils
from heatclient import exc as heat_exc
from heatclient.openstack.common._i18n import _
from heatclient.openstack.common._i18n import _LI


def _authenticated_fetcher(client):
    def _do(*args, **kwargs):
        if isinstance(client.http_client, http.SessionClient):
            method, url = args
            return client.http_client.request(url, method, **kwargs).content
        else:
            return client.http_client.raw_request(*args, **kwargs).content

    return _do


class CreateStack(show.ShowOne):
    """Create a stack."""

    log = logging.getLogger(__name__ + '.CreateStack')

    def get_parser(self, prog_name):
        parser = super(CreateStack, self).get_parser(prog_name)
        parser.add_argument(
            '-t', '--template',
            metavar='<template>',
            required=True,
            help=_('Path to the template')
        )
        parser.add_argument(
            '-e', '--environment',
            metavar='<environment>',
            action='append',
            help=_('Path to the environment. Can be specified multiple times')
        )
        parser.add_argument(
            '--timeout',
            metavar='<timeout>',
            type=int,
            help=_('Stack creating timeout in minutes')
        )
        parser.add_argument(
            '--pre-create',
            metavar='<resource>',
            default=None,
            action='append',
            help=_('Name of a resource to set a pre-create hook to. Resources '
                   'in nested stacks can be set using slash as a separator: '
                   'nested_stack/another/my_resource. You can use wildcards '
                   'to match multiple stacks or resources: '
                   'nested_stack/an*/*_resource. This can be specified '
                   'multiple times')
        )
        parser.add_argument(
            '--enable-rollback',
            action='store_true',
            help=_('Enable rollback on create/update failure')
        )
        parser.add_argument(
            '--parameter',
            metavar='<key=value>',
            action='append',
            help=_('Parameter values used to create the stack. This can be '
                   'specified multiple times')
        )
        parser.add_argument(
            '--parameter-file',
            metavar='<key=file>',
            action='append',
            help=_('Parameter values from file used to create the stack. '
                   'This can be specified multiple times. Parameter values '
                   'would be the content of the file')
        )
        parser.add_argument(
            '--wait',
            action='store_true',
            help=_('Wait until stack goes to CREATE_COMPLETE or CREATE_FAILED')
        )
        parser.add_argument(
            '--tags',
            metavar='<tag1,tag2...>',
            help=_('A list of tags to associate with the stack')
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help=_('Do not actually perform the stack create, but show what '
                   'would be created')
        )
        parser.add_argument(
            'name',
            metavar='<stack-name>',
            help=_('Name of the stack to create')
        )

        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)

        client = self.app.client_manager.orchestration

        tpl_files, template = template_utils.process_template_path(
            parsed_args.template,
            object_request=_authenticated_fetcher(client))

        env_files, env = (
            template_utils.process_multiple_environments_and_files(
                env_paths=parsed_args.environment))

        parameters = heat_utils.format_all_parameters(
            parsed_args.parameter,
            parsed_args.parameter_file,
            parsed_args.template)

        if parsed_args.pre_create:
            template_utils.hooks_to_env(env, parsed_args.pre_create,
                                        'pre-create')

        fields = {
            'stack_name': parsed_args.name,
            'disable_rollback': not parsed_args.enable_rollback,
            'parameters': parameters,
            'template': template,
            'files': dict(list(tpl_files.items()) + list(env_files.items())),
            'environment': env
        }

        if parsed_args.tags:
            fields['tags'] = parsed_args.tags
        if parsed_args.timeout:
            fields['timeout_mins'] = parsed_args.timeout

        if parsed_args.dry_run:
            stack = client.stacks.preview(**fields)

            formatters = {
                'description': heat_utils.text_wrap_formatter,
                'template_description': heat_utils.text_wrap_formatter,
                'stack_status_reason': heat_utils.text_wrap_formatter,
                'parameters': heat_utils.json_formatter,
                'outputs': heat_utils.json_formatter,
                'resources': heat_utils.json_formatter,
                'links': heat_utils.link_formatter,
            }

            columns = []
            for key in stack.to_dict():
                columns.append(key)
            columns.sort()

            return (
                columns,
                utils.get_item_properties(stack, columns,
                                          formatters=formatters)
            )

        stack = client.stacks.create(**fields)['stack']
        if parsed_args.wait:
            stack_status, msg = event_utils.poll_for_events(
                client, parsed_args.name, action='CREATE')
            if stack_status == 'CREATE_FAILED':
                raise exc.CommandError(msg)

        return _show_stack(client, stack['id'], format='table', short=True)


class UpdateStack(show.ShowOne):
    """Update a stack."""

    log = logging.getLogger(__name__ + '.UpdateStack')

    def get_parser(self, prog_name):
        parser = super(UpdateStack, self).get_parser(prog_name)
        parser.add_argument(
            '-t', '--template', metavar='<template>',
            help=_('Path to the template')
        )
        parser.add_argument(
            '-e', '--environment', metavar='<environment>',
            action='append',
            help=_('Path to the environment. Can be specified multiple times')
        )
        parser.add_argument(
            '--pre-update', metavar='<resource>', action='append',
            help=_('Name of a resource to set a pre-update hook to. Resources '
                   'in nested stacks can be set using slash as a separator: '
                   'nested_stack/another/my_resource. You can use wildcards '
                   'to match multiple stacks or resources: '
                   'nested_stack/an*/*_resource. This can be specified '
                   'multiple times')
        )
        parser.add_argument(
            '--timeout', metavar='<timeout>', type=int,
            help=_('Stack update timeout in minutes')
        )
        parser.add_argument(
            '--rollback', metavar='<value>',
            help=_('Set rollback on update failure. '
                   'Value "enabled" sets rollback to enabled. '
                   'Value "disabled" sets rollback to disabled. '
                   'Value "keep" uses the value of existing stack to be '
                   'updated (default)')
        )
        parser.add_argument(
            '--dry-run', action="store_true",
            help=_('Do not actually perform the stack update, but show what '
                   'would be changed')
        )
        parser.add_argument(
            '--parameter', metavar='<key=value>',
            help=_('Parameter values used to create the stack. '
                   'This can be specified multiple times'),
            action='append'
        )
        parser.add_argument(
            '--parameter-file', metavar='<key=file>',
            help=_('Parameter values from file used to create the stack. '
                   'This can be specified multiple times. Parameter value '
                   'would be the content of the file'),
            action='append'
        )
        parser.add_argument(
            '--existing', action="store_true",
            help=_('Re-use the template, parameters and environment of the '
                   'current stack. If the template argument is omitted then '
                   'the existing template is used. If no %(env_arg)s is '
                   'specified then the existing environment is used. '
                   'Parameters specified in %(arg)s will patch over the '
                   'existing values in the current stack. Parameters omitted '
                   'will keep the existing values') % {
                       'arg': '--parameter', 'env_arg': '--environment'}
        )
        parser.add_argument(
            '--clear-parameter', metavar='<parameter>',
            help=_('Remove the parameters from the set of parameters of '
                   'current stack for the %(cmd)s. The default value in the '
                   'template will be used. This can be specified multiple '
                   'times') % {'cmd': 'stack-update'},
            action='append'
        )
        parser.add_argument(
            'stack', metavar='<stack>',
            help=_('Name or ID of stack to update')
        )
        parser.add_argument(
            '--tags', metavar='<tag1,tag2...>',
            help=_('An updated list of tags to associate with the stack')
        )
        parser.add_argument(
            '--wait',
            action='store_true',
            help=_('Wait until stack goes to UPDATE_COMPLETE or '
                   'UPDATE_FAILED')
        )

        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)

        client = self.app.client_manager.orchestration

        tpl_files, template = template_utils.process_template_path(
            parsed_args.template,
            object_request=_authenticated_fetcher(client),
            existing=parsed_args.existing)

        env_files, env = (
            template_utils.process_multiple_environments_and_files(
                env_paths=parsed_args.environment))

        parameters = heat_utils.format_all_parameters(
            parsed_args.parameter,
            parsed_args.parameter_file,
            parsed_args.template)

        if parsed_args.pre_update:
            template_utils.hooks_to_env(env, parsed_args.pre_update,
                                        'pre-update')

        fields = {
            'stack_id': parsed_args.stack,
            'parameters': parameters,
            'existing': parsed_args.existing,
            'template': template,
            'files': dict(list(tpl_files.items()) + list(env_files.items())),
            'environment': env
        }

        if parsed_args.tags:
            fields['tags'] = parsed_args.tags
        if parsed_args.timeout:
            fields['timeout_mins'] = parsed_args.timeout
        if parsed_args.clear_parameter:
            fields['clear_parameters'] = list(parsed_args.clear_parameter)

        if parsed_args.rollback:
            rollback = parsed_args.rollback.strip().lower()
            if rollback not in ('enabled', 'disabled', 'keep'):
                msg = _('--rollback invalid value: %s') % parsed_args.rollback
                raise exc.CommandError(msg)
            if rollback != 'keep':
                fields['disable_rollback'] = rollback == 'disabled'

        if parsed_args.dry_run:
            changes = client.stacks.preview_update(**fields)

            fields = ['state', 'resource_name', 'resource_type',
                      'resource_identity']

            columns = sorted(changes.get("resource_changes", {}).keys())
            data = [heat_utils.json_formatter(changes["resource_changes"][key])
                    for key in columns]

            return columns, data

        if parsed_args.wait:
            # find the last event to use as the marker
            events = event_utils.get_events(client,
                                            stack_id=parsed_args.stack,
                                            event_args={'sort_dir': 'desc',
                                                        'limit': 1})
            marker = events[0].id if events else None

        client.stacks.update(**fields)

        if parsed_args.wait:
            stack = client.stacks.get(parsed_args.stack)
            stack_status, msg = event_utils.poll_for_events(
                client, stack.stack_name, action='UPDATE', marker=marker)
            if stack_status == 'UPDATE_FAILED':
                raise exc.CommandError(msg)

        return _show_stack(client, parsed_args.stack, format='table',
                           short=True)


class ShowStack(show.ShowOne):
    """Show stack details."""

    log = logging.getLogger(__name__ + ".ShowStack")

    def get_parser(self, prog_name):
        parser = super(ShowStack, self).get_parser(prog_name)
        parser.add_argument(
            'stack',
            metavar='<stack>',
            help='Stack to display (name or ID)',
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug("take_action(%s)", parsed_args)

        heat_client = self.app.client_manager.orchestration
        return _show_stack(heat_client, stack_id=parsed_args.stack,
                           format=parsed_args.formatter)


def _show_stack(heat_client, stack_id, format='', short=False):
    try:
        data = heat_client.stacks.get(stack_id=stack_id)
    except heat_exc.HTTPNotFound:
        raise exc.CommandError('Stack not found: %s' % stack_id)
    else:

        columns = [
            'id',
            'stack_name',
            'description',
            'creation_time',
            'updated_time',
            'stack_status',
            'stack_status_reason',
        ]

        if not short:
            columns += [
                'parameters',
                'outputs',
                'links',
            ]

            exclude_columns = ('template_description',)
            for key in data.to_dict():
                # add remaining columns without an explicit order
                if key not in columns and key not in exclude_columns:
                    columns.append(key)

        formatters = {}
        complex_formatter = None
        if format in 'table':
            complex_formatter = heat_utils.yaml_formatter
        elif format in ('shell', 'value', 'html'):
            complex_formatter = heat_utils.json_formatter
        if complex_formatter:
            formatters['parameters'] = complex_formatter
            formatters['outputs'] = complex_formatter
            formatters['links'] = complex_formatter
            formatters['tags'] = complex_formatter

        return columns, utils.get_item_properties(data, columns,
                                                  formatters=formatters)


class ListStack(lister.Lister):
    """List stacks."""

    log = logging.getLogger(__name__ + '.ListStack')

    def get_parser(self, prog_name):
        parser = super(ListStack, self).get_parser(prog_name)
        parser.add_argument(
            '--deleted',
            action='store_true',
            help=_('Include soft-deleted stacks in the stack listing')
        )
        parser.add_argument(
            '--nested',
            action='store_true',
            help=_('Include nested stacks in the stack listing')
        )
        parser.add_argument(
            '--hidden',
            action='store_true',
            help=_('Include hidden stacks in the stack listing')
        )
        parser.add_argument(
            '--property',
            dest='properties',
            metavar='<key=value>',
            help=_('Filter properties to apply on returned stacks (repeat to '
                   'filter on multiple properties)'),
            action=parseractions.KeyValueAction
        )
        parser.add_argument(
            '--tags',
            metavar='<tag1,tag2...>',
            help=_('List of tags to filter by. Can be combined with '
                   '--tag-mode to specify how to filter tags')
        )
        parser.add_argument(
            '--tag-mode',
            metavar='<mode>',
            help=_('Method of filtering tags. Must be one of "any", "not", '
                   'or "not-any". If not specified, multiple tags will be '
                   'combined with the boolean AND expression')
        )
        parser.add_argument(
            '--limit',
            metavar='<limit>',
            help=_('The number of stacks returned')
        )
        parser.add_argument(
            '--marker',
            metavar='<id>',
            help=_('Only return stacks that appear after the given ID')
        )
        parser.add_argument(
            '--sort',
            metavar='<key>[:<direction>]',
            help=_('Sort output by selected keys and directions (asc or desc) '
                   '(default: asc). Specify multiple times to sort on '
                   'multiple properties')
        )
        parser.add_argument(
            '--all-projects',
            action='store_true',
            help=_('Include all projects (admin only)')
        )
        parser.add_argument(
            '--short',
            action='store_true',
            help=_('List fewer fields in output')
        )
        parser.add_argument(
            '--long',
            action='store_true',
            help=_('List additional fields in output, this is implied by '
                   '--all-projects')
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug("take_action(%s)", parsed_args)

        client = self.app.client_manager.orchestration
        return _list(client, args=parsed_args)


def _list(client, args=None):
    kwargs = {}
    columns = [
        'ID',
        'Stack Name',
        'Stack Status',
        'Creation Time',
        'Updated Time',
    ]

    if args:
        kwargs = {'limit': args.limit,
                  'marker': args.marker,
                  'filters': heat_utils.format_parameters(args.properties),
                  'tags': None,
                  'tags_any': None,
                  'not_tags': None,
                  'not_tags_any': None,
                  'global_tenant': args.all_projects or args.long,
                  'show_deleted': args.deleted,
                  'show_hidden': args.hidden}

        if args.tags:
            if args.tag_mode:
                if args.tag_mode == 'any':
                    kwargs['tags_any'] = args.tags
                elif args.tag_mode == 'not':
                    kwargs['not_tags'] = args.tags
                elif args.tag_mode == 'not-any':
                    kwargs['not_tags_any'] = args.tags
                else:
                    err = _('tag mode must be one of "any", "not", "not-any"')
                    raise exc.CommandError(err)
            else:
                kwargs['tags'] = args.tags

        if args.short:
            columns.pop()
            columns.pop()
        if args.long:
            columns.insert(2, 'Stack Owner')
        if args.long or args.all_projects:
            columns.insert(2, 'Project')

        if args.nested:
            columns.append('Parent')
            kwargs['show_nested'] = True

    data = client.stacks.list(**kwargs)
    data = utils.sort_items(data, args.sort if args else None)

    return (
        columns,
        (utils.get_item_properties(s, columns) for s in data)
    )


class DeleteStack(command.Command):
    """Delete stack(s)."""

    log = logging.getLogger(__name__ + ".DeleteStack")

    def get_parser(self, prog_name):
        parser = super(DeleteStack, self).get_parser(prog_name)
        parser.add_argument(
            'stack',
            metavar='<stack>',
            nargs='+',
            help=_('Stack(s) to delete (name or ID)')
        )
        parser.add_argument(
            '--yes',
            action='store_true',
            help=_('Skip yes/no prompt (assume yes)')
        )
        parser.add_argument(
            '--wait',
            action='store_true',
            help=_('Wait for stack delete to complete')
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug("take_action(%s)", parsed_args)

        heat_client = self.app.client_manager.orchestration

        try:
            if not parsed_args.yes and sys.stdin.isatty():
                sys.stdout.write(
                    _("Are you sure you want to delete this stack(s) [y/N]? "))
                prompt_response = sys.stdin.readline().lower()
                if not prompt_response.startswith('y'):
                    self.log.info(_LI('User did not confirm stack delete so '
                                      'taking no action.'))
                    return
        except KeyboardInterrupt:  # ctrl-c
            self.log.info(_LI('User did not confirm stack delete '
                              '(ctrl-c) so taking no action.'))
            return
        except EOFError:  # ctrl-d
            self.log.info(_LI('User did not confirm stack delete '
                              '(ctrl-d) so taking no action.'))
            return

        failure_count = 0
        stacks_waiting = []
        for sid in parsed_args.stack:
            marker = None
            if parsed_args.wait:
                try:
                    # find the last event to use as the marker
                    events = event_utils.get_events(heat_client,
                                                    stack_id=sid,
                                                    event_args={
                                                        'sort_dir': 'desc',
                                                        'limit': 1})
                    if events:
                        marker = events[0].id
                except heat_exc.CommandError as ex:
                    failure_count += 1
                    print(ex)
                    continue

            try:
                heat_client.stacks.delete(sid)
                stacks_waiting.append((sid, marker))
            except heat_exc.HTTPNotFound:
                failure_count += 1
                print(_('Stack not found: %s') % sid)
            except heat_exc.Forbidden:
                failure_count += 1
                print(_('Forbidden: %s') % sid)

        if parsed_args.wait:
            for sid, marker in stacks_waiting:
                try:
                    stack_status, msg = event_utils.poll_for_events(
                        heat_client, sid, action='DELETE', marker=marker)
                except heat_exc.CommandError:
                    continue
                if stack_status == 'DELETE_FAILED':
                    failure_count += 1
                    print(msg)

        if failure_count:
            msg = (_('Unable to delete %(count)d of the %(total)d stacks.') %
                   {'count': failure_count, 'total': len(parsed_args.stack)})
            raise exc.CommandError(msg)


class AdoptStack(show.ShowOne):
    """Adopt a stack."""

    log = logging.getLogger(__name__ + '.AdoptStack')

    def get_parser(self, prog_name):
        parser = super(AdoptStack, self).get_parser(prog_name)
        parser.add_argument(
            'name',
            metavar='<stack-name>',
            help=_('Name of the stack to adopt')
        )
        parser.add_argument(
            '-e', '--environment',
            metavar='<environment>',
            action='append',
            help=_('Path to the environment. Can be specified multiple times')
        )
        parser.add_argument(
            '--timeout',
            metavar='<timeout>',
            type=int,
            help=_('Stack creation timeout in minutes')
        )
        parser.add_argument(
            '--adopt-file',
            metavar='<adopt-file>',
            required=True,
            help=_('Path to adopt stack data file')
        )
        parser.add_argument(
            '--enable-rollback',
            action='store_true',
            help=_('Enable rollback on create/update failure')
        )
        parser.add_argument(
            '--parameter',
            metavar='<key=value>',
            action='append',
            help=_('Parameter values used to create the stack. Can be '
                   'specified multiple times')
        )
        parser.add_argument(
            '--wait',
            action='store_true',
            help=_('Wait until stack adopt completes')
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)

        client = self.app.client_manager.orchestration

        env_files, env = (
            template_utils.process_multiple_environments_and_files(
                env_paths=parsed_args.environment))

        adopt_url = heat_utils.normalise_file_path_to_url(
            parsed_args.adopt_file)
        adopt_data = request.urlopen(adopt_url).read().decode('utf-8')

        fields = {
            'stack_name': parsed_args.name,
            'disable_rollback': not parsed_args.enable_rollback,
            'adopt_stack_data': adopt_data,
            'parameters': heat_utils.format_parameters(parsed_args.parameter),
            'files': dict(list(env_files.items())),
            'environment': env,
            'timeout': parsed_args.timeout
        }

        stack = client.stacks.create(**fields)['stack']

        if parsed_args.wait:
            stack_status, msg = event_utils.poll_for_events(
                client, parsed_args.name, action='ADOPT')
            if stack_status == 'ADOPT_FAILED':
                raise exc.CommandError(msg)

        return _show_stack(client, stack['id'], format='table', short=True)


class AbandonStack(format_utils.JsonFormat):
    """Abandon stack and output results."""

    log = logging.getLogger(__name__ + '.AbandonStack')

    def get_parser(self, prog_name):
        parser = super(AbandonStack, self).get_parser(prog_name)
        parser.add_argument(
            'stack',
            metavar='<stack>',
            help=_('Name or ID of stack to abandon')
        )
        parser.add_argument(
            '--output-file',
            metavar='<output-file>',
            help=_('File to output abandon results')
        )

        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)

        client = self.app.client_manager.orchestration

        try:
            stack = client.stacks.abandon(stack_id=parsed_args.stack)
        except heat_exc.HTTPNotFound:
            msg = _('Stack not found: %s') % parsed_args.stack
            raise exc.CommandError(msg)

        if parsed_args.output_file is not None:
            try:
                with open(parsed_args.output_file, 'w') as f:
                    f.write(jsonutils.dumps(stack, indent=2))
                    return [], None
            except IOError as e:
                raise exc.CommandError(str(e))

        data = list(six.itervalues(stack))
        columns = list(six.iterkeys(stack))
        return columns, data


class OutputShowStack(show.ShowOne):
    """Show stack output."""

    log = logging.getLogger(__name__ + '.OutputShowStack')

    def get_parser(self, prog_name):
        parser = super(OutputShowStack, self).get_parser(prog_name)
        parser.add_argument(
            'stack',
            metavar='<stack>',
            help=_('Name or ID of stack to query')
        )
        parser.add_argument(
            'output',
            metavar='<output>',
            nargs='?',
            default=None,
            help=_('Name of an output to display')
        )
        parser.add_argument(
            '--all',
            action='store_true',
            help=_('Display all stack outputs')
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)

        client = self.app.client_manager.orchestration

        if not parsed_args.all and parsed_args.output is None:
            msg = _('Either <OUTPUT NAME> or --all must be specified.')
            raise exc.CommandError(msg)

        if parsed_args.all and parsed_args.output is not None:
            msg = _('Cannot specify both <OUTPUT NAME> and --all.')
            raise exc.CommandError(msg)

        if parsed_args.all:
            try:
                stack = client.stacks.get(parsed_args.stack)
            except heat_exc.HTTPNotFound:
                msg = _('Stack not found: %s') % parsed_args.stack
                raise exc.CommandError(msg)

            outputs = stack.to_dict().get('outputs', [])
            columns = []
            values = []
            for output in outputs:
                columns.append(output['output_key'])
                values.append(heat_utils.json_formatter(output))

            return columns, values

        try:
            output = client.stacks.output_show(parsed_args.stack,
                                               parsed_args.output)['output']
        except heat_exc.HTTPNotFound:
            msg = _('Stack %(id)s or output %(out)s not found.') % {
                'id': parsed_args.stack, 'out': parsed_args.output}
            raise exc.CommandError(msg)

        if 'output_error' in output:
            msg = _('Output error: %s') % output['output_error']
            raise exc.CommandError(msg)

        if (isinstance(output['output_value'], list) or
                isinstance(output['output_value'], dict)):
            output['output_value'] = heat_utils.json_formatter(
                output['output_value'])

        return self.dict2columns(output)


class OutputListStack(lister.Lister):
    """List stack outputs."""

    log = logging.getLogger(__name__ + '.OutputListStack')

    def get_parser(self, prog_name):
        parser = super(OutputListStack, self).get_parser(prog_name)
        parser.add_argument(
            'stack',
            metavar='<stack>',
            help=_('Name or ID of stack to query')
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)

        client = self.app.client_manager.orchestration

        try:
            outputs = client.stacks.output_list(parsed_args.stack)['outputs']
        except heat_exc.HTTPNotFound:
            msg = _('Stack not found: %s') % parsed_args.stack
            raise exc.CommandError(msg)

        columns = ['output_key', 'description']

        return (
            columns,
            (utils.get_dict_properties(s, columns) for s in outputs)
        )


class TemplateShowStack(format_utils.YamlFormat):
    """Display stack template."""

    log = logging.getLogger(__name__ + '.TemplateShowStack')

    def get_parser(self, prog_name):
        parser = super(TemplateShowStack, self).get_parser(prog_name)
        parser.add_argument(
            'stack',
            metavar='<stack>',
            help=_('Name or ID of stack to query')
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug('take_action(%s)', parsed_args)

        client = self.app.client_manager.orchestration

        try:
            template = client.stacks.template(stack_id=parsed_args.stack)
        except heat_exc.HTTPNotFound:
            msg = _('Stack not found: %s') % parsed_args.stack
            raise exc.CommandError(msg)

        return self.dict2columns(template)


class StackActionBase(lister.Lister):
    """Stack actions base."""

    log = logging.getLogger(__name__ + '.StackActionBase')

    def _get_parser(self, prog_name, stack_help, wait_help):
        parser = super(StackActionBase, self).get_parser(prog_name)
        parser.add_argument(
            'stack',
            metavar='<stack>',
            nargs="+",
            help=stack_help
        )
        parser.add_argument(
            '--wait',
            action='store_true',
            help=wait_help
        )
        return parser

    def _take_action(self, parsed_args, action, action_name=None):
        self.log.debug("take_action(%s)", parsed_args)
        heat_client = self.app.client_manager.orchestration
        return _stacks_action(
            parsed_args,
            heat_client,
            action,
            action_name
        )


def _stacks_action(parsed_args, heat_client, action, action_name=None):
    rows = []
    columns = [
        'ID',
        'Stack Name',
        'Stack Status',
        'Creation Time',
        'Updated Time'
    ]
    for stack in parsed_args.stack:
        data = _stack_action(stack, parsed_args, heat_client, action,
                             action_name)
        rows += [utils.get_dict_properties(data.to_dict(), columns)]
    return (columns, rows)


def _stack_action(stack, parsed_args, heat_client, action, action_name=None):
    if parsed_args.wait:
        # find the last event to use as the marker
        events = event_utils.get_events(heat_client,
                                        stack_id=stack,
                                        event_args={'sort_dir': 'desc',
                                                    'limit': 1})
        marker = events[0].id if events else None

    try:
        action(stack)
    except heat_exc.HTTPNotFound:
        msg = _('Stack not found: %s') % stack
        raise exc.CommandError(msg)

    if parsed_args.wait:
        s = heat_client.stacks.get(stack)
        stack_status, msg = event_utils.poll_for_events(
            heat_client, s.stack_name, action=action_name, marker=marker)
        if action_name:
            if stack_status == '%s_FAILED' % action_name:
                raise exc.CommandError(msg)
        else:
            if stack_status.endswith('_FAILED'):
                raise exc.CommandError(msg)

    return heat_client.stacks.get(stack)


class SuspendStack(StackActionBase):
    """Suspend a stack."""

    log = logging.getLogger(__name__ + '.SuspendStack')

    def get_parser(self, prog_name):
        return self._get_parser(
            prog_name,
            _('Stack(s) to suspend (name or ID)'),
            _('Wait for suspend to complete')
        )

    def take_action(self, parsed_args):
        return self._take_action(
            parsed_args,
            self.app.client_manager.orchestration.actions.suspend,
            'SUSPEND'
        )


class ResumeStack(StackActionBase):
    """Resume a stack."""

    log = logging.getLogger(__name__ + '.ResumeStack')

    def get_parser(self, prog_name):
        return self._get_parser(
            prog_name,
            _('Stack(s) to resume (name or ID)'),
            _('Wait for resume to complete')
        )

    def take_action(self, parsed_args):
        return self._take_action(
            parsed_args,
            self.app.client_manager.orchestration.actions.resume,
            'RESUME'
        )


class CheckStack(StackActionBase):
    """Check a stack."""

    log = logging.getLogger(__name__ + '.CheckStack')

    def get_parser(self, prog_name):
        return self._get_parser(
            prog_name,
            _('Stack(s) to check update (name or ID)'),
            _('Wait for check to complete')
        )

    def take_action(self, parsed_args):
        return self._take_action(
            parsed_args,
            self.app.client_manager.orchestration.actions.check,
            'CHECK'
        )


class CancelStack(StackActionBase):
    """Cancel current task for a stack.

    Supported tasks for cancellation:
    * update
    """

    log = logging.getLogger(__name__ + '.CancelStack')

    def get_parser(self, prog_name):
        return self._get_parser(
            prog_name,
            _('Stack(s) to cancel (name or ID)'),
            _('Wait for check to complete')
        )

    def take_action(self, parsed_args):
        self.log.debug("take_action(%s)", parsed_args)
        rows = []
        columns = [
            'ID',
            'Stack Name',
            'Stack Status',
            'Creation Time',
            'Updated Time'
        ]
        heat_client = self.app.client_manager.orchestration

        for stack in parsed_args.stack:
            try:
                data = heat_client.stacks.get(stack_id=stack)
            except heat_exc.HTTPNotFound:
                raise exc.CommandError('Stack not found: %s' % stack)

            status = getattr(data, 'stack_status').lower()
            if status == 'update_in_progress':
                data = _stack_action(
                    stack,
                    parsed_args,
                    heat_client,
                    heat_client.actions.cancel_update
                )
                rows += [utils.get_dict_properties(data.to_dict(), columns)]
            else:
                err = _("Stack %(id)s with status \'%(status)s\' "
                        "not in cancelable state") % {
                    'id': stack, 'status': status}
                raise exc.CommandError(err)

        return (columns, rows)


class StackHookPoll(lister.Lister):
    '''List resources with pending hook for a stack.'''

    log = logging.getLogger(__name__ + '.StackHookPoll')

    def get_parser(self, prog_name):
        parser = super(StackHookPoll, self).get_parser(prog_name)
        parser.add_argument(
            'stack',
            metavar='<stack>',
            help=_('Stack to display (name or ID)')
        )
        parser.add_argument(
            '--nested-depth',
            metavar='<nested-depth>',
            help=_('Depth of nested stacks from which to display hooks')
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug("take_action(%s)", parsed_args)
        heat_client = self.app.client_manager.orchestration
        return _hook_poll(
            parsed_args,
            heat_client
        )


def _hook_poll(args, heat_client):
    """List resources with pending hook for a stack."""

    # There are a few steps to determining if a stack has pending hooks
    # 1. The stack is IN_PROGRESS status (otherwise, by definition no hooks
    #    can be pending
    # 2. There is an event for a resource associated with hitting a hook
    # 3. There is not an event associated with clearing the hook in step(2)
    #
    # So, essentially, this ends up being a specially filtered type of event
    # listing, because all hook status is exposed via events.  In future
    # we might consider exposing some more efficient interface via the API
    # to reduce the expense of this brute-force polling approach
    columns = ['ID', 'Resource Status Reason', 'Resource Status', 'Event Time']

    if args.nested_depth:
        try:
            nested_depth = int(args.nested_depth)
        except ValueError:
            msg = _("--nested-depth invalid value %s") % args.nested_depth
            raise exc.CommandError(msg)
        columns.append('Stack Name')
    else:
        nested_depth = 0

    hook_type = hook_utils.get_hook_type_via_status(heat_client, args.stack)
    event_args = {'sort_dir': 'asc'}
    hook_events = event_utils.get_hook_events(
        heat_client, stack_id=args.stack, event_args=event_args,
        nested_depth=nested_depth, hook_type=hook_type)

    if len(hook_events) >= 1:
        if hasattr(hook_events[0], 'resource_name'):
            columns.insert(0, 'Resource Name')
        else:
            columns.insert(0, 'Logical Resource ID')

    rows = (utils.get_item_properties(h, columns) for h in hook_events)
    return (columns, rows)


class StackHookClear(command.Command):
    """Clear resource hooks on a given stack."""

    log = logging.getLogger(__name__ + '.StackHookClear')

    def get_parser(self, prog_name):
        parser = super(StackHookClear, self).get_parser(prog_name)
        parser.add_argument(
            'stack',
            metavar='<stack>',
            help=_('Stack to display (name or ID)')
        )
        parser.add_argument(
            '--pre-create',
            action='store_true',
            help=_('Clear the pre-create hooks')
        )
        parser.add_argument(
            '--pre-update',
            action='store_true',
            help=_('Clear the pre-update hooks')
        )
        parser.add_argument(
            'hook',
            metavar='<resource>',
            nargs='+',
            help=_('Resource names with hooks to clear. Resources '
                   'in nested stacks can be set using slash as a separator: '
                   'nested_stack/another/my_resource. You can use wildcards '
                   'to match multiple stacks or resources: '
                   'nested_stack/an*/*_resource')
        )
        return parser

    def take_action(self, parsed_args):
        self.log.debug("take_action(%s)", parsed_args)
        heat_client = self.app.client_manager.orchestration
        return _hook_clear(
            parsed_args,
            heat_client
        )


def _hook_clear(args, heat_client):
    """Clear resource hooks on a given stack."""
    if args.pre_create:
        hook_type = 'pre-create'
    elif args.pre_update:
        hook_type = 'pre-update'
    else:
        hook_type = hook_utils.get_hook_type_via_status(heat_client,
                                                        args.stack)

    for hook_string in args.hook:
        hook = [b for b in hook_string.split('/') if b]
        resource_pattern = hook[-1]
        stack_id = args.stack

        hook_utils.clear_wildcard_hooks(heat_client, stack_id, hook[:-1],
                                        hook_type, resource_pattern)
