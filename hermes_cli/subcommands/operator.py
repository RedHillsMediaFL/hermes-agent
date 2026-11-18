"""The normal local-owner approval entry point for native app setup."""
from __future__ import annotations


def build_operator_parser(subparsers):
    from hermes_cli.agent_setup.cli import command
    parser = subparsers.add_parser('operator', help='Approve or revoke scoped app operator access')
    actions = parser.add_subparsers(dest='operator_action', required=True)
    actions.add_parser('list', help='List pending app-instance approval requests')
    for name in ['approve', 'deny']:
        action = actions.add_parser(name, help=name.capitalize() + ' one pending app request')
        action.add_argument('code', help='The eight-character verification code displayed by Agent')
    revoke = actions.add_parser('revoke', help='Revoke one operator grant')
    revoke.add_argument('grant_id')
    parser.set_defaults(func=command)
