import os
from typing import List, Iterable, Set, Union, Optional, TYPE_CHECKING, TextIO, Tuple

if TYPE_CHECKING:
    from .scoreSheetBot import ScoreSheetBot
from fuzzywuzzy import process, fuzz
import asyncio
import discord
from discord.ext import commands
from .battle import *
import time
from .constants import *
from .crew import Crew

Context = discord.ext.commands.Context


def key_string(ctx: Context) -> str:
    return str(ctx.guild) + '|' + str(ctx.channel.id)


def channel_id_from_key(key: str) -> int:
    return int(key[key.index("|") + 1:])


async def update_channel_open(prefix: str, channel: discord.TextChannel):
    if channel.name.startswith(YES) or channel.name.startswith(NO):
        new_name = prefix + channel.name[1:]
    else:
        new_name = prefix + channel.name
    try:
        await asyncio.wait_for(channel.edit(name=new_name), timeout=2)
    except asyncio.TimeoutError:
        return


def escape(string: str) -> str:
    special = ['\\', '>', '`', '_', '*', '|']
    out = string[:]
    for char in special:
        if char in out:
            out = out.replace(char, '\\' + char)
    return out


def split_embed(embed: discord.Embed, length: int) -> List[discord.Embed]:
    ret = []
    desc = embed.description
    desc_split = split_on_length_and_separator(desc, length, '\n')
    ret.append(discord.Embed(title=embed.title, color=embed.color, description=desc_split.pop(0)))

    for split in desc_split:
        ret.append(discord.Embed(color=embed.color, description=split))
    return ret


def split_on_length_and_separator(string: str, length: int, separator: str) -> List[str]:
    ret = []
    while len(string) > length:
        idx = length - 1
        while string[idx] != separator:
            if idx == 0:
                raise ValueError
            idx -= 1
        ret.append(string[:idx + 1])
        string = string[idx + 1:]
    ret.append(string)
    return ret


async def send_long(ctx: Context, message: str, sep: str):
    output = split_on_length_and_separator(message, length=2000, separator=sep)
    for put in output:
        await ctx.send(put)


async def send_long_embed(ctx: Context, message: discord.Embed):
    output = split_embed(message, length=2000)
    for put in output:
        await ctx.send(embed=put)


def is_usable_emoji(text: str, bot):
    if text.startswith('<:'):
        text = text[2:]
        if text.endswith('>'):
            text = text[:-1]
        name = text[:text.index(':')]
        emoji_id = text[text.index(':') + 1:]
        emoji = discord.utils.get(bot.emojis, name=name)
        if emoji:
            return emoji.available
    return False


def check_roles(user: discord.Member, roles: Iterable) -> bool:
    return any((role.name in roles for role in user.roles))


async def send_sheet(channel: Union[discord.TextChannel, Context], battle: Battle):
    embed_split = split_embed(embed=battle.embed(), length=2000)
    if battle.battle_over():
        if not all(battle.confirms):
            footer = ''
            footer += '\nPlease confirm: '
            if battle.mock:
                footer += 'anyone can confirm or clear a mock.'
            else:
                if not battle.confirms[0]:
                    footer += f'\n {battle.team1.name}: '
                    for leader in battle.team1.leader:
                        footer += f'{leader}, '
                    footer = footer[:-2]
                    footer += ' please `,confirm`.'
                if not battle.confirms[1]:
                    footer += f'\n {battle.team2.name}: '
                    for leader in battle.team2.leader:
                        footer += f'{leader}, '
                    footer = footer[:-2]
                    footer += ' please `,confirm`.'
            await channel.send(footer)
    for embed in embed_split:
        await channel.send(embed=embed)


def crew(user: discord.Member, bot: 'ScoreSheetBot') -> Optional[str]:
    roles = user.roles
    if any((role.name == OVERFLOW_ROLE for role in roles)):
        overflow_user = bot.cache.overflow_server.get_member(user.id)
        if overflow_user:
            roles = overflow_user.roles

    for role in roles:
        if role.name in bot.cache.crews:
            return role.name
    raise ValueError(f'{str(user)} has no crew or something is wrong.')


async def track_cycle(user: discord.Member, scs: discord.Guild) -> int:
    track = -1
    if check_roles(user, [TRUE_LOCKED]):
        return 3
    for i in range(len(TRACK)):
        if check_roles(user, [TRACK[i]]):
            track = i
    if 0 <= track < 2:
        old_track = discord.utils.get(scs.roles, name=TRACK[track])
        await user.remove_roles(old_track, reason='Left a crew, moved up the track.')
    if track < 2:
        new_track = discord.utils.get(scs.roles, name=TRACK[track + 1])
        await user.add_roles(new_track, reason='User left a crew, moved up the track.')
    return track + 1


def power_level(user: discord.Member):
    if check_roles(user, STAFF_LIST):
        return 3
    if check_roles(user, [LEADER]):
        return 2
    if check_roles(user, [ADVISOR]):
        return 1
    return 0


def compare_crew_and_power(author: discord.Member, target: discord.Member, bot: 'ScoreSheetBot') -> None:
    author_pl = power_level(author)
    if author_pl == 3:
        return
    author_crew = crew(author, bot)
    target_crew = crew(target, bot)
    if author_crew is not target_crew:
        raise ValueError(
            f'{author.display_name} on {author_crew} cannot unflair {target.display_name} on {target_crew}')
    target_pl = power_level(target)
    if author_pl == 2:
        if check_roles(target, [LEADER]):
            raise ValueError(
                f'A majority of leaders must approve unflairing leader {target.mention}.'
                f' Tag the Doc Keeper role in {bot.cache.channels.flairing_questions} for assistance.')
        return

    if author_pl == 1:
        if target_pl >= author_pl:
            raise ValueError(
                f' cannot unflair {target.mention} as you are not powerful enough.')
        return

    raise ValueError('You must be an advisor, leader or staff to unflair others.')


def user_by_id(name: str, bot: 'ScoreSheetBot') -> discord.Member:
    if len(name) < 17:
        raise ValueError(f'{name} is not a mention or an id. Try again.')
    try:
        id = int(name.strip("<!@>"))
    except ValueError:
        raise ValueError(f'{name} is not a mention or an id. Try again.')
    user = bot.cache.scs.get_member(id)
    if user:
        return user
    raise ValueError(f'{name} doesn\'t seem to be on this server or your input is malformed. Try @user.')


def member_lookup(name: str, bot: 'ScoreSheetBot') -> Optional[discord.Member]:
    if len(name) >= 17:
        if (name.startswith('<') and name.endswith('>')) or name.isdigit():
            return user_by_id(name, bot)
    true_name = process.extractOne(name, bot.cache.main_members.keys(), scorer=fuzz.ratio, score_cutoff=30)
    if true_name:
        return bot.cache.main_members[true_name[0]]
    else:
        raise ValueError(f'{name} does not match any member in the server.')


def crew_lookup(crew_str: str, bot: 'ScoreSheetBot') -> Optional[Crew]:
    if crew_str.lower() in bot.cache.crews_by_tag:
        return bot.cache.crews_by_tag[crew_str.lower()]
    true_crew = process.extractOne(crew_str, bot.cache.crews_by_name.keys(), score_cutoff=40)
    if true_crew:
        return bot.cache.crews_by_name[true_crew[0]]
    else:
        raise ValueError(f'{crew_str} does not match any crew in the server.')


def ambiguous_lookup(name: str, bot: 'ScoreSheetBot') -> Union[discord.Member, Crew]:
    if name.lower() in bot.cache.crews_by_tag:
        return bot.cache.crews_by_tag[name.lower()]
    if len(name) >= 17:
        if (name.startswith('<') and name.endswith('>')) or name.isdigit():
            return user_by_id(name, bot)

    true_name = process.extractOne(name, bot.cache.main_members.keys(), scorer=fuzz.ratio)
    true_crew = process.extractOne(name, bot.cache.crews_by_name.keys(), scorer=fuzz.ratio)
    if true_crew[1] >= true_name[1]:
        return bot.cache.crews_by_name[true_crew[0]]
    else:
        return bot.cache.main_members[true_name[0]]


def strip_non_ascii(text: str) -> str:
    encoded_string = text.encode("ascii", "ignore")
    decode_string = encoded_string.decode()
    return decode_string


def add_join_cd(member: discord.Member, file: TextIO):
    file.write(f'{member.id} {time.time() + COOLDOWN_TIME_SECONDS}\n')


async def flair(member: discord.Member, flairing_crew: Crew, bot: 'ScoreSheetBot', staff: bool = False):
    if check_roles(member, [TRUE_LOCKED]):
        raise ValueError(f'{member.mention} cannot be flaired because they are {TRUE_LOCKED}.')

    if check_roles(member, [JOIN_CD]):
        raise ValueError(f'{member.mention} cannot be flaired because they have {JOIN_CD}.')
    if not staff:
        if check_roles(member, [POWER_MERGE]):
            raise ValueError(f'{member.mention} cannot be flaired because they are a potential power merge.\n'
                             f'Please tag the Doc Keeper role in '
                             f'{bot.cache.channels.flairing_questions.mention} to confirm.')
        if check_roles(member, [FLAIR_VERIFY]):
            raise ValueError(f'{member.mention} needs to be verified before flairing. \n'
                             f'Please tag the Doc Keeper role in '
                             f'{bot.cache.channels.flairing_questions.mention} to confirm.')

    if check_roles(member, [FREE_AGENT]):
        await member.remove_roles(bot.cache.roles.free_agent, reason=f'Flaired for {flairing_crew.name}')
    if flairing_crew.overflow:
        await member.add_roles(bot.cache.roles.overflow)
        overflow_crew = discord.utils.get(bot.cache.overflow_server.roles, name=flairing_crew.name)
        overflow_member = discord.utils.get(bot.cache.overflow_server.members, id=member.id)
        await overflow_member.add_roles(overflow_crew)
        member_nick = nick_without_prefix(member.nick) if member.nick else nick_without_prefix(member.name)
        await member.edit(nick=f'{flairing_crew.abbr} | {member_nick}')
    else:
        main_crew = discord.utils.get(bot.cache.scs.roles, name=flairing_crew.name)
        await member.add_roles(main_crew)
    if check_roles(member, [TRACK[2]]):
        await member.remove_roles(bot.cache.roles.track3)
        await member.add_roles(bot.cache.roles.true_locked)
        pepper = discord.utils.get(bot.cache.scs.members, id=456156481067286529)
        flairing_info = bot.cache.channels.flairing_info
        await flairing_info.send(f'{pepper.mention} {member.mention} is {TRUE_LOCKED}.')
    await member.add_roles(bot.cache.roles.join_cd)
    await member.add_roles(bot.cache.roles.playoff)
    add_join_cd(member, open(TEMP_ROLES_FILE, 'a'))


async def unflair(member: discord.Member, author: discord.member, bot: 'ScoreSheetBot'):
    user_crew = crew(member, bot)
    if check_roles(member, [bot.cache.roles.overflow.name]):
        user = discord.utils.get(bot.cache.overflow_server.members, id=member.id)

        await member.edit(nick=nick_without_prefix(member.display_name))
        role = discord.utils.get(bot.cache.overflow_server.roles, name=user_crew)
        overflow_adv = discord.utils.get(bot.cache.overflow_server.roles, name=ADVISOR)
        overflow_leader = discord.utils.get(bot.cache.overflow_server.roles, name=LEADER)
        await user.remove_roles(role, overflow_adv, overflow_leader, reason=f'Unflaired by {author.name}')
        await member.remove_roles(bot.cache.roles.overflow, reason=f'Unflaired by {author.name}')
    else:
        role = discord.utils.get(bot.cache.scs.roles, name=user_crew)
        await member.remove_roles(role, reason=f'Unflaired by {author.name}')
    if await track_cycle(member, bot.cache.scs) == 2:
        pepper = discord.utils.get(bot.cache.scs.members, id=456156481067286529)
        flairing_info = bot.cache.channels.flairing_info
        await flairing_info.send(f'{pepper.mention} {member.mention} is locked on next join.')
    await member.remove_roles(bot.cache.roles.advisor, bot.cache.roles.leader,
                              reason=f'Unflaired by {author.name}')


def nick_without_prefix(nick: str) -> str:
    if '|' in nick:
        return nick[nick.rindex('|') + 1:]
    else:
        return nick


def role_change(before: Set[discord.Role], after: Set[discord.Role], changer: discord.Member,
                changee: discord.Member, of_before: Optional[Set[discord.Role]] = None,
                of_after: Optional[Set[discord.Role]] = None) -> discord.Embed:
    removed = before - after
    added = after - before
    of_string = []
    if of_before and of_after:
        of_removed = of_before - of_after
        of_added = of_after - of_before
        of_string.append('\nOverflow:\nRoles Removed: ')
        for role in of_removed:
            of_string.append(f'{role.name}, ')
        if of_removed:
            of_string[-1] = of_string[-1][:-2]  # Trim extra comma and space
        of_string.append('\nRoles Added: ')
        for role in of_added:
            of_string.append(f'{role.name}, ')
        if of_added:
            of_string[-1] = of_string[-1][:-2]  # Trim extra comma and space
    header = f'Flairing Change: {str(changee)}'
    body = [f'Mention: {changee.mention}\n', f'ID: {changee.id}\n', 'Roles Removed: ']
    for role in removed:
        body.append(f'{role.name}, ')
    if removed:
        body[-1] = body[-1][:-2]  # Trim extra comma and space
    body.append('\nRoles Added: ')
    for role in added:
        body.append(f'{role.name}, ')
    if added:
        body[-1] = body[-1][:-2]  # Trim extra comma and space
    body.extend(of_string)
    body.append(f'\nChanges Made By: {str(changer)} {changer.id}')

    return discord.Embed(title=header, description=''.join(body), color=changee.color)


async def promote(member: discord.Member, bot: 'ScoreSheetBot') -> str:
    if check_roles(member, [LEADER]):
        return 'Leader'
    if check_roles(member, [ADVISOR]):
        await member.add_roles(bot.cache.roles.leader)
        await member.remove_roles(bot.cache.roles.advisor)
        return 'Leader'
    await member.add_roles(bot.cache.roles.advisor)
    return 'Advisor'


async def demote(member: discord.Member, bot: 'ScoreSheetBot') -> str:
    if check_roles(member, [LEADER]):
        await member.remove_roles(bot.cache.roles.leader)
        await member.add_roles(bot.cache.roles.advisor)
        return 'Leader to Advisor'
    if check_roles(member, [ADVISOR]):
        await member.remove_roles(bot.cache.roles.advisor)
        return 'Advisor to Member'
    return ''


async def response_message(ctx: Context, msg: str):
    msg = await ctx.send(f'{ctx.author.mention}: {msg}')
    await ctx.message.delete(delay=1)
    return msg


def crew_members(crew_input: Crew, bot: 'ScoreSheetBot') -> List[discord.Member]:
    members = []
    for member in bot.cache.scs.members:
        try:
            cr = crew(member, bot)
        except ValueError:
            cr = None
        if cr == crew_input.name:
            members.append(member)
    return members


def split_possibilities(two_things: str, sep: Optional[str] = ' ') -> List[Tuple[str, str]]:
    split = two_things.split(sep)
    out = []
    for i in range(len(split)):
        out.append((' '.join(split[:i]), (' '.join(split[i:]))))
    return out


def best_of_possibilities(combined: str, bot: 'ScoreSheetBot'):
    pos = split_possibilities(combined)
    all_role_names = {role.name for role in bot.cache.scs.roles}
    all_role_names = set.union(set(bot.cache.crews), all_role_names)
    best = ['', '', 0]
    for sep in pos:
        if sep[0].lower() in bot.cache.crews_by_tag:
            sep = (bot.cache.crews_by_tag[sep[0].lower()].name, sep[1])

        if sep[1].lower() in bot.cache.crews_by_tag:
            sep = (sep[0], bot.cache.crews_by_tag[sep[1].lower()].name)
        first, second = search_two_roles_in_list(sep[0], sep[1], all_role_names)
        value = first[1] + second[1]
        if value > best[2]:
            best = [first[0], second[0], value]
    return best


def search_two_roles_in_list(first_role: str, second_role: str, everything):
    first = process.extractOne(first_role, everything)
    second = process.extractOne(second_role, everything)
    return first, second


def overlap_members(first: str, second: str, bot: 'ScoreSheetBot') -> List[discord.Member]:
    crew_role = None
    other_role = None
    if first in bot.cache.crews:
        if second in bot.cache.crews:
            raise ValueError(f'Interpreted as {first} and {second}. '
                             f'You can\'t have members on two crews! Try to be more specific.')
        crew_role = first
        other_role = second
    if second in bot.cache.crews:
        crew_role = second
        other_role = first
    out = []
    if crew_role:
        for member in bot.cache.scs.members:
            try:
                if crew(member, bot) == crew_role:
                    for role in member.roles:
                        if role.name == other_role:
                            out.append(member)
            except ValueError:
                continue
    else:
        for member in bot.cache.scs.members:
            role_names = {role.name for role in member.roles}
            if first in role_names and second in role_names:
                out.append(member)
    return out


async def wait_for_reaction_on_message(confirm: str, cancel: Optional[str],
                                       message: discord.Message, author: discord.Member, bot: discord.Client) -> bool:
    await message.add_reaction(confirm)
    await message.add_reaction(cancel)

    def check(reaction, user):
        return user == author and str(reaction.emoji) == confirm or cancel

    while True:
        try:
            react, reactor = await bot.wait_for('reaction_add', timeout=30.0, check=check)
        except asyncio.TimeoutError:
            return False
        if str(react.emoji) == confirm and reactor == author:
            return True
        elif str(react.emoji) == cancel and reactor == author:
            return False


async def cooldown_process(bot: 'ScoreSheetBot') -> List[str]:
    current_cooldown = set()
    with open(TEMP_ROLES_FILE, 'r') as file:
        lines = file.readlines()
        out = []
        current = time.time()
        for line in lines:
            if len(line) > 17:
                member_id = int(line[:line.index(' ')])
                reset = float(line[line.index(' ') + 1:-1])
                member = bot.cache.scs.get_member(member_id)
                current_cooldown.add(member_id)
                diff = reset - current
                hours = int(diff // 3600)
                minutes = int((diff % 3600) // 60)
                seconds = int(diff % 60)
                out.append(f'{str(member)} has {hours} hours, {minutes} minutes, {seconds} seconds'
                           f'  left on their join cooldown.')
    for person in bot.cache.scs.members:
        if check_roles(person, [JOIN_CD]):
            if person.id not in current_cooldown:
                await person.remove_roles(bot.cache.roles.join_cd)
                await bot.cache.channels.flair_log.send(f'{person.display_name}\'s join cooldown ended.')
    return out


async def cache_process(bot: 'ScoreSheetBot'):
    await bot.cache.update(bot)
    if os.getenv('VERSION') == 'PROD':
        await cooldown_process(bot)
    for key in bot.battle_map:
        channel = bot.cache.scs.get_channel(channel_id_from_key(key))
        if bot.battle_map[key]:
            await update_channel_open(NO, channel)
