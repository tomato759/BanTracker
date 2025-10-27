import math
import json
import time
import discord
import logging
import asyncio
from discord.ext import tasks
from typing import List, Optional
from datetime import datetime, timedelta

try:
    import aiohttp
except ImportError:
    import requests as reqs
    USING_AIOHTTP = False
else:
    USING_AIOHTTP = True


class JSONConfig:
    def __init__(self, file_name: str) -> None:
        self.file_name = file_name
        with open(file_name) as conf:
            self.config = json.load(conf)

    def update(self, key: str, value):
        self.config[key] = value
        with open(self.file_name, "w") as conf:
            json.dump(self.config, conf, indent=4)

    def get(self, key: str, default=None):
        return self.config.get(key, default)


class BanTracker:
    def __init__(self) -> None:
        self.owd_bans = None
        self.ostaff_bans = None
        self.total_wd_tracked = 0
        self.total_staff_tracked = 0
        self.start_time = time.time()
        self.last_fetch_time = None
        self.consecutive_errors = 0
        
        if USING_AIOHTTP:
            self.session = None
        else:
            self.session = reqs.Session()
            self.session.headers.update({"User-Agent": "H"})

    async def init_session(self):
        if USING_AIOHTTP and self.session is None:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": "H"}
            )

    async def close_session(self):
        if USING_AIOHTTP and self.session:
            await self.session.close()

    async def check_bans(self):
        try:
            if USING_AIOHTTP:
                async with self.session.get('https://api.plancke.io/hypixel/v1/punishmentStats') as resp:
                    data = await resp.json()
            else:
                data = await asyncio.to_thread(
                    lambda: self.session.get('https://api.plancke.io/hypixel/v1/punishmentStats').json()
                )
            
            curr_stats = data.get('record')
            if not curr_stats:
                return []
            
            wd_bans = curr_stats.get("watchdog_total")
            staff_bans = curr_stats.get("staff_total")
            self.last_fetch_time = time.time()
            self.consecutive_errors = 0
            embeds = []

            if self.owd_bans is not None and self.ostaff_bans is not None:
                wban_dif = wd_bans - self.owd_bans
                sban_dif = staff_bans - self.ostaff_bans

                if wban_dif > 0:
                    self.total_wd_tracked += wban_dif
                    plural = "s" if wban_dif != 1 else ""
                    embeds.append(f"🐶 Watchdog banned {wban_dif} player{plural}! (Total tracked: {self.total_wd_tracked:,})")

                if sban_dif > 0:
                    self.total_staff_tracked += sban_dif
                    plural = "s" if sban_dif != 1 else ""
                    embeds.append(f"👮 Staff banned {sban_dif} player{plural}! (Total tracked: {self.total_staff_tracked:,})")

            self.owd_bans = wd_bans
            self.ostaff_bans = staff_bans
            return embeds
            
        except Exception as e:
            self.consecutive_errors += 1
            logging.getLogger('discord').error(f"Error fetching ban data: {e}")
            return []

    def get_stats_embed(self) -> discord.Embed:
        uptime = time.time() - self.start_time
        uptime_str = str(timedelta(seconds=int(uptime)))
        
        embed = discord.Embed(
            title="📊 Ban Tracker Statistics",
            color=discord.Color.blue(),
            timestamp=datetime.now(datetime.UTC)
        )
        embed.add_field(name="⚔️ Watchdog Bans Tracked", value=f"{self.total_wd_tracked:,}", inline=True)
        embed.add_field(name="👮 Staff Bans Tracked", value=f"{self.total_staff_tracked:,}", inline=True)
        embed.add_field(name="📈 Total Tracked", value=f"{self.total_wd_tracked + self.total_staff_tracked:,}", inline=True)
        embed.add_field(name="⏰ Uptime", value=uptime_str, inline=True)
        
        if self.last_fetch_time:
            last_check = f"<t:{math.floor(self.last_fetch_time)}:R>"
            embed.add_field(name="🔄 Last Check", value=last_check, inline=True)
        
        if self.owd_bans and self.ostaff_bans:
            embed.add_field(name="🎯 Current Total Bans", value=f"{self.owd_bans + self.ostaff_bans:,}", inline=True)
        
        return embed


class BanTrackerBot(discord.Client):
    def __init__(self, intents: discord.Intents) -> None:
        super().__init__(intents=intents)
        self.tree = discord.app_commands.CommandTree(self)
        self.jsonconfig = JSONConfig("config.json")
        self.bantracker = BanTracker()
        self.logger = logging.getLogger('discord')
        self.channel_ids = []

        @self.event
        async def on_ready():
            await self.bantracker.init_session()
            self.channel_ids = self.jsonconfig.get("channels", [])
            
            invalid_channels = []
            for channel_id in self.channel_ids[:]:
                if self.get_channel(channel_id) is None:
                    invalid_channels.append(channel_id)
                    self.channel_ids.remove(channel_id)
            
            if invalid_channels:
                self.jsonconfig.update("channels", self.channel_ids)
                self.logger.warning(f"Removed {len(invalid_channels)} invalid channel(s)")
            
            for guild in self.guilds:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            
            plural = "s" if len(self.guilds) != 1 else ""
            self.logger.info(f"Synced commands with {len(self.guilds)} guild{plural}.")
            self.logger.info(f"Monitoring {len(self.channel_ids)} channel(s)")
            check_loop.start()

        @self.event
        async def on_guild_join(guild):
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            self.logger.info(f"Synced commands with {guild.name}.")

        @self.tree.command()
        async def subscribe(interaction: discord.Interaction):
            """Subscribes the channel to receive ban notifications"""
            if not interaction.user.guild_permissions.manage_channels:
                await interaction.response.send_message(
                    "> ❌ You need the `Manage Channels` permission to use this command.",
                    ephemeral=True
                )
                return
            
            if interaction.channel_id not in self.channel_ids:
                self.channel_ids.append(interaction.channel_id)
                self.jsonconfig.update("channels", self.channel_ids)
                self.logger.info(f"{interaction.channel.name} in {interaction.guild.name} was subscribed.")
                await interaction.response.send_message("> ✅ This channel is now subscribed to ban notifications.")
            else:
                await interaction.response.send_message("> ℹ️ This channel is already subscribed.")

        @self.tree.command()
        async def unsubscribe(interaction: discord.Interaction):
            """Unsubscribes the channel from receiving ban notifications"""
            if not interaction.user.guild_permissions.manage_channels:
                await interaction.response.send_message(
                    "> ❌ You need the `Manage Channels` permission to use this command.",
                    ephemeral=True
                )
                return
            
            if interaction.channel_id in self.channel_ids:
                self.channel_ids.remove(interaction.channel_id)
                self.jsonconfig.update("channels", self.channel_ids)
                self.logger.info(f"{interaction.channel.name} in {interaction.guild.name} was unsubscribed.")
                await interaction.response.send_message("> ✅ This channel is no longer subscribed.")
            else:
                await interaction.response.send_message("> ℹ️ This channel is not subscribed.")

        @self.tree.command()
        async def stats(interaction: discord.Interaction):
            """Shows statistics about the ban tracker"""
            embed = self.bantracker.get_stats_embed()
            await interaction.response.send_message(embed=embed)

        @self.tree.command()
        async def list_channels(interaction: discord.Interaction):
            """Lists all subscribed channels (Admin only)"""
            if not interaction.user.guild_permissions.administrator:
                await interaction.response.send_message(
                    "> ❌ You need the `Administrator` permission to use this command.",
                    ephemeral=True
                )
                return
            
            if not self.channel_ids:
                await interaction.response.send_message("> ℹ️ No channels are currently subscribed.")
                return
            
            embed = discord.Embed(
                title="📋 Subscribed Channels",
                color=discord.Color.green(),
                description=""
            )
            
            for channel_id in self.channel_ids:
                channel = self.get_channel(channel_id)
                if channel:
                    embed.description += f"• {channel.mention} ({channel.guild.name})\n"
                else:
                    embed.description += f"• Unknown Channel (ID: {channel_id})\n"
            
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @tasks.loop(seconds=30)
        async def check_loop():
            bans = await self.bantracker.check_bans()
            if bans:
                failed_channels = []
                for channel_id in self.channel_ids:
                    channel = self.get_channel(channel_id)
                    if channel:
                        try:
                            for message in bans:
                                await channel.send(message)
                        except discord.Forbidden:
                            self.logger.warning(f"No permission to send to channel {channel_id}")
                            failed_channels.append(channel_id)
                        except discord.HTTPException as e:
                            self.logger.error(f"Failed to send to channel {channel_id}: {e}")
                    else:
                        failed_channels.append(channel_id)
                
                if failed_channels:
                    for channel_id in failed_channels:
                        if channel_id in self.channel_ids:
                            self.channel_ids.remove(channel_id)
                    self.jsonconfig.update("channels", self.channel_ids)
                    self.logger.info(f"Removed {len(failed_channels)} failed channel(s)")

    async def close(self):
        await self.bantracker.close_session()
        await super().close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    intents = discord.Intents.default()
    discordbot = BanTrackerBot(intents)
    
    try:
        discordbot.run(discordbot.jsonconfig.config.get("token"))
    except KeyboardInterrupt:
        logging.info("Shutting down...")
    finally:
        asyncio.run(discordbot.close())
