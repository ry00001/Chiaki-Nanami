import aiohttp
import discord
import functools
import re
import socket
import struct

from collections import namedtuple
from discord.ext import commands
from itertools import zip_longest

from . import utils
from ..utils import checks
from ..utils.database import Database
from ..utils.misc import pairwise


# I would like to tank rjt.rockx (aka Obliterator) for providing me information
# on how the diep.io party links work. Without him, this wouldn't be possible

def _swap_pairs(s):
    it = iter(s)
    return "".join([b+a for a, b in zip_longest(it, it, fillvalue='')])

SERVERS_URL = "http://lb.diep.io/v2/find_servers"
async def _load_servers_from_site():
    async with aiohttp.get(SERVERS_URL) as response:
        data = await response.text()
    servers = pairwise(data[2:].split('\x00'))
    return [DiepioServer(*s) for s in servers]

async def _produce_server_list():
    global SERVER_LIST
    SERVER_LIST = await _load_servers_from_site()

def _search(pattern, string):
    try:
        return re.search(pattern, string).group(1)
    except AttributeError:
        return None

class DiepioServer(namedtuple('DiepioServer', 'ip_port name')):
    _translations = {
        'teams'   : '2-TDM',
        '4teams'  : '4-TDM',
        'dom'     : 'Domination',
        'maze'    : 'Maze',
        'sandbox' : 'Sandbox',
        }

    @discord.utils.cached_property
    def _server(self):
        return re.match(r'([a-z]*)-([a-z]*):?(.*):', self.name).groups()

    @discord.utils.cached_property
    def _ip_port(self):
        return re.match(r'(.*):(.*)', self.ip_port).groups()

    @property
    def ip(self):
        return self._ip_port[0]

    @property
    def port(self):
        return self._ip_port[1]

    @property
    def company(self):
        return self._server[0].title()

    @property
    def location(self):
        return self._server[1]

    @property
    def mode(self):
        return self._translations.get(self._server[2], 'FFA')

class LinkServerData(namedtuple('LinkServerData', 'code server')):
    def is_sandbox(self):
        print(type(self.code_length), self.code_length)
        return self.server.mode == 'Sandbox' or self.code_length > 20

    def is_tdm(self):
        return self.server.mode.endswith('TDM')

    def format(self):
        return ("Code: {0.code}\n"
                "Code length: {0.code_length}\n"
                "IP: {1.ip}\n"
                "Mode: {1.mode}\n"
                "Company: {1.company}\n"
                "Location: {1.location}\n"
                ).format(self, self.server)

    @property
    def code_length(self):
        return len(self.code)

    @property
    def link(self):
        return "http://diep.io/#" + self.code

    # This is wrong.
    # For some reason, sandbox links can be 24 chars
    # Which makes the last few chars even more confusing
##    @property
##    def room(self):
##        return int(self.code[-2:], 16) if self.is_sandbox() else None

    @property
    def embed(self):
        attrs = ["code", "code_length",]
        server_attrs = ["ip", "mode", "company", "location",]
        embed = discord.Embed(title=self.link,
                              colour=utils.mode_colour(self.server.mode))
        for attr in attrs:
            embed.add_field(name=attr.title(), value=getattr(self, attr))
        for sattr in server_attrs:
            embed.add_field(name=sattr.title(), value=getattr(self.server, sattr))
        return embed

def _find_server_by_ip(ip):
    return discord.utils.get(SERVER_LIST, ip=ip)

def _hex_to_int(hexs):
    return int(hexs, 16)

def _ip_from_hex(hexs):
    new_hex = _swap_pairs(hexs)
    addr_long = _hex_to_int(new_hex)
    return socket.inet_ntoa(struct.pack("<L", addr_long)[::-1])

def _extract_links(message):
    return re.findall(r'(diep.io/#[1234567890ABCDEF]*)\s?', message)

def _extract_code(link):
    return _search(r'diep.io/#([1234567890ABCDEF]*)\s?', link)

def _is_valid_hex(s):
    try:
        _hex_to_int(s)
    except ValueError:
        return False
    else:
        return True

def _is_valid_party_code(code):
    code_len = len(code)
    if code_len % 2:
        return False
    if not 20 <= code_len <= 24:
        return False
    return _is_valid_hex(code)

def read_link(link):
    code = _extract_code(link)
    if not _is_valid_party_code(code):
        return None
    is_sandbox = len(code) == 22
    ip = _ip_from_hex(code[:8])
    server = _find_server_by_ip(ip)
    if server is None:
        return None
    return LinkServerData(code, server)

class PartyLinks:
    def __init__(self, bot):
        self.bot = bot
        config_default = lambda: {"detect" : True, "delete" : True, 'tdm_delete': False}
        self.pl_config_db = Database.from_json("plconfig.json", default_factory=config_default)
        self.pl_allowed = Database.from_json("plimmune.json", default_factory=list)
        self.bot.loop.create_task(_produce_server_list())

    def _is_immune(self, message):
        immune_roles = self.pl_allowed[message.server]
        if not immune_roles:
            return False
        getter = functools.partial(discord.utils.get, message.author.roles)
        return any(getter(id=id) is not None for id in immune_roles)

    async def _handle_delete(self, message, message_string=None, *, silent=False):
        if self._is_immune(message):
            return
        if not silent:
            await self.bot.send_message(message.channel, message_string.format(msg=message))
        await self.bot.delete_message(message)

    async def on_message(self, message):
        server = message.server
        config = self.pl_config_db[server]
        links = _extract_links(message.content)
        if not links:
            return
        link_data = list(filter(None, map(read_link, links)))

        if config.get('all_delete'):
            await self._handle_delete(message, silent=True)
            return

        # Prevent posting TDM links in public
        if config.get("tdm_delete") and any(data.is_tdm() for data in link_data):
            await self._handle_delete(message, "{msg.author.mention} TDM links are not allowed here.")
            return

        # Prevent posting sandbox links in public
        # Posting links in public chats never seems to end well so I've created a guard for it
        if config["delete"] and any(data.is_sandbox() for data in link_data):
            await self._handle_delete(message, "{msg.author.mention} Please DM (direct message) "
                                               "your sandbox links, unless you want Arena Closers")
            return

        data_formats = [data.format() for data in link_data]
        if config["detect"] and data_formats:
            pld = "**__PARTY LINK{} DETECTED!__**\n".format('s' * (len(links) != 1))
            await self.bot.send_message(message.channel, pld, embed=link_data[0].embed)
            # Let's hope there's a way to send multiple embeds in one message soon
            for em in (data.embed for data in link_data[1:]):
                await self.bot.send_message(message.channel, embed=em)

    @commands.group(hidden=True, aliases=['plset'])
    @checks.is_admin()
    async def partylinkset(self):
        pass

    @partylinkset.command(pass_context=True, aliases=['plra'])
    @checks.is_admin()
    async def linkroleadd(self, ctx, *, role: discord.Role):
        """Adds a role to the list of roles that are allowed to post party-links on this server"""
        self.pl_allowed[ctx.message.server].append(role.id)
        await self.bot.say(f"Successfully allowed **{role}** to post party links!")

    @partylinkset.command(pass_context=True, aliases=['plrd'])
    @checks.is_admin()
    async def linkroledelete(self, ctx, *, role: discord.Role):
        """Removes a role to the list of roles that are allowed to post party-links on this server"""
        try:
            self.pl_allowed[ctx.message.server].remove(role.id)
        except ValueError:
            await self.bot.say(f"**{role}** was never allowed to post party links in the first place...")
        else:
            await self.bot.say(f"Successfully disallowed **{role}** to post party links!")

    @partylinkset.command(pass_context=True)
    @checks.is_admin()
    async def detect(self, ctx, mode: bool):
        """Configures if party link detection (and the posting of info relating to the link) should be deleted"""
        self.pl_config_db[ctx.message.server]["detect"] = mode
        await self.bot.say(f"Party link detection {'deins'[mode::2]}abled, I think")

    @partylinkset.command(pass_context=True)
    @checks.is_admin()
    async def delete(self, ctx, mode: bool):
        """Configures if all party links should be deleted"""
        self.pl_config_db[ctx.message.server]["all_delete"] = mode
        await self.bot.say(f"Party Link deletion {'deins'[mode::2]}abled, I think")


    @partylinkset.command(pass_context=True)
    @checks.is_admin()
    async def sbdelete(self, ctx, mode: bool):
        """Configures if sandbox party links should be deleted"""
        self.pl_config_db[ctx.message.server]["delete"] = mode
        await self.bot.say(f"Sandbox link deletion {'deins'[mode::2]}abled, I think")

    @partylinkset.command(pass_context=True)
    @checks.is_admin()
    async def tdmdelete(self, ctx, mode: bool):
        """Configures if TDM party links should be deleted"""
        self.pl_config_db[ctx.message.server]["tdm_delete"] = mode
        await self.bot.say(f"TDM (2 Teams and 4 Teams) deletion {'deins'[mode::2]}abled, I think")

def setup(bot):
    bot.add_cog(PartyLinks(bot), hidden=True)
