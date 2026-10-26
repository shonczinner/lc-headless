# WanderPunch - Pure combat bot: scout for spots, fight to death, respawn, repeat
# Trains attack/strength/defence to target levels (lowest below target first).

STYLE_ATTACK = 'attack'
STYLE_STRENGTH = 'strength'
STYLE_DEFENCE = 'defence'


def available_modes():
    """The wielded weapon's mode for each melee style, as {style: mode}. Inlined from duel_arena.training."""
    modes = {}
    for style in (STYLE_ATTACK, STYLE_STRENGTH, STYLE_DEFENCE):
        resolution = game.combat_style_resolution(style)
        if resolution is not None and resolution.effective == style:
            modes[style] = resolution.mode
    return modes

# Custom target_style: trains the absolute lowest skill below target (including defence)
def target_style(attack, strength, defence, target_attack, target_strength, target_defence):
    """Train the lowest skill below its target. Includes Defence."""
    skills_below = []
    if attack < target_attack:
        skills_below.append(('attack', attack))
    if strength < target_strength:
        skills_below.append(('strength', strength))
    if defence < target_defence:
        skills_below.append(('defence', defence))
    
    if not skills_below:
        return 'attack'  # all targets reached
    
    # Return the skill with lowest current level
    lowest = min(skills_below, key=lambda x: x[1])
    return lowest[0]
from rs2004.catalogs import DeathRecovery

SETTINGS = {
    'target_names': SettingDef('string', '', 'NPC names to attack (comma separated; empty = any attackable)'),
    'wander_radius': SettingDef('number', 8, min=3, max=20, label='Short wander radius (fallback)'),
    'search_radius': SettingDef('number', 35, min=10, max=50, label='Search radius for targets'),
    'min_npc_level': SettingDef('number', 0, min=0, max=126, label='Skip NPCs below this combat level (0=disabled, only max cap applies)'),
    'prefer_high_level': SettingDef('boolean', True, 'Prefer higher-level NPCs (more XP)'),
    'target_attack': SettingDef('number', 99, min=1, max=99, label='Target Attack level'),
    'target_strength': SettingDef('number', 99, min=1, max=99, label='Target Strength level'),
    'target_defence': SettingDef('number', 99, min=1, max=99, label='Target Defence level'),
    'max_fight_ticks': SettingDef('number', 600, min=50, max=1000, label='Max ticks fighting same target before giving up'),
    'abysmal_minutes': SettingDef('number', 3, min=1, max=10, label='Minutes with no XP before forced rescout'),
    'max_npc_level_ratio': SettingDef('number', 0.5, min=0.1, max=1.0, label='Max NPC level as ratio of combat level'),
    'verbose_location': SettingDef('boolean', True, 'Log detailed location info'),
    # Scout & Settle
    'scout_radius': SettingDef('number', 60, min=20, max=100, label='Radius to scout for combat spots'),
    'min_npcs_per_spot': SettingDef('number', 3, min=1, max=20, label='Minimum NPCs to consider a spot'),
    'max_players_per_spot': SettingDef('number', 2, min=0, max=10, label='Max players in spot area'),
    'spot_score_min': SettingDef('number', 5, min=1, max=50, label='Minimum score to settle (npc_count * avg_level - player_penalty)'),
    'player_penalty': SettingDef('number', 10, min=1, max=50, label='Penalty per player in spot'),
}

COMBAT_SKILLS = ['attack', 'strength', 'defence', 'hitpoints']
ABYSMAL_TICKS = 100  # ticks (approx 1 min) per minute


class WanderPunch(TaskBot):
    def on_start(self):
        self.kills = 0
        self.deaths = 0
        self.start_tile = game.tile()
        self.xp_start = sum([skills.xp(s) for s in COMBAT_SKILLS])
        self.last_xp = self.xp_start
        self.last_xp_time = game.tick()
        self.target = None
        self.fight_start_tick = 0
        self.no_target_streak = 0
        self.last_success_tile = self.start_tile
        self.skip_until = {}

        s = self.settings
        self.wander_r = int(s.wander_radius)
        self.search_r = int(s.search_radius)
        self.min_npc_level = int(s.min_npc_level)
        self.prefer_high = bool(s.prefer_high_level)
        self.target_attack = int(s.target_attack)
        self.target_strength = int(s.target_strength)
        self.target_defence = int(s.target_defence)
        self.max_fight_ticks = int(s.max_fight_ticks)
        self.abysmal_ticks = int(s.abysmal_minutes) * ABYSMAL_TICKS
        self.max_npc_level_ratio = float(s.max_npc_level_ratio)
        self.verbose_location = bool(s.verbose_location)
        self.scout_r = int(s.scout_radius)
        self.min_npcs_per_spot = int(s.min_npcs_per_spot)
        self.max_players_per_spot = int(s.max_players_per_spot)
        self.spot_score_min = int(s.spot_score_min)
        self.player_penalty = int(s.player_penalty)

        names = str(s.target_names).strip()
        if names:
            self.target_names = [n.strip().lower() for n in names.split(',') if n.strip()]
        else:
            self.target_names = []

        log('WanderPunch starting')

        # Task order: style -> fight -> wander
        # Only one action runs per tick (first matching condition)
        self.add(
            Task(self.style_wrong, self.set_style, 'style'),
            Task(self.should_fight, self.do_fight, 'fight'),
            Task(lambda: True, self.wander, 'wander'),
        )

        # Death recovery: walk back to last success tile
        self.death_recovery = DeathRecovery(
            self, self.start_tile, 6,
            on_death=self._on_death,
            on_recovered=self._on_recovered,
            walk_back=self._walk_back_to_success,
        )
        self.add(self.death_recovery)

    def _on_death(self):
        self.deaths += 1
        self.target = None
        self.fight_start_tick = 0
        if self.verbose_location:
            log('died at ' + str(game.tile()) + '; will return to last success spot')

    def _on_recovered(self):
        self.target = None
        self.fight_start_tick = 0
        self.start_tile = self.last_success_tile

    def _walk_back_to_success(self):
        if self.verbose_location:
            log('walking back to last success: ' + str(self.last_success_tile))
        yield from traversal.walk_resilient(self.last_success_tile, radius=4, timeout_ms=120000, log=lambda m: None)

    def desired_style(self):
        atk = skills.level('attack')
        strn = skills.level('strength')
        deff = skills.level('defence')
        style = target_style(atk, strn, deff, self.target_attack, self.target_strength, self.target_defence)
        if self.verbose_location:
            log('target_style: atk={} str={} def={} -> {}'.format(atk, strn, deff, style))
        return style

    def style_wrong(self):
        if game.in_combat():
            return False
        style = self.desired_style()
        modes = available_modes()
        has = game.has_combat_style(style)
        if self.verbose_location:
            log('style_wrong: style={} in_modes={} has={}'.format(style, style in modes, has))
        return style not in modes or not has

    def set_style(self):
        style = self.desired_style()
        modes = available_modes()
        if style not in modes:
            if self.verbose_location:
                log('set_style: style {} not in available modes'.format(style))
            yield from execution.delay_ticks(1)
            return
        if game.has_combat_style(style):
            return
        if self.verbose_location:
            log('setting combat style to {}'.format(style))
        game.set_combat_style(style)
        yield from execution.delay_until(lambda: game.has_combat_style(style), 3000)

    def should_fight(self):
        """Return True only if we have a valid target to attack RIGHT NOW."""
        if game.in_combat():
            # Already fighting - let do_fight handle it
            return True

        # Track XP for abysmal check
        now = game.tick()
        cur_xp = sum([skills.xp(s) for s in COMBAT_SKILLS])
        if cur_xp > self.last_xp:
            self.last_xp = cur_xp
            self.last_xp_time = now

        # Force rescout if no XP for too long
        if now - self.last_xp_time > self.abysmal_ticks and self.last_xp_time > 0:
            if self.verbose_location:
                log('abysmal XP - forcing rescout')
            self.no_target_streak = max(self.no_target_streak, 10)
            return False

        # Find a target to attack
        npc = self.find_target()
        if npc:
            return True
        return False

    def _combat_level(self):
        atk = skills.level('attack')
        strn = skills.level('strength')
        deff = skills.level('defence')
        hp = skills.level('hitpoints')
        return int(0.25 * (deff + hp) + 0.325 * (atk + strn))

    def find_target(self):
        """Find best valid target in search radius. Returns NPC or None."""
        now = game.tick()

        # Re-validate current target
        if self.target is not None:
            t = npcs.get(self.target)
            if t is not None and t.valid():
                if t.targets_me() or not t.in_combat:
                    if (self.target_names and (t.name or '').lower() in self.target_names) or (not self.target_names and t.level >= self.min_npc_level):
                        if self.skip_until.get(t.index, 0) <= now:
                            return t
            self.target = None

        combat_lvl = self._combat_level()
        max_npc_lvl = max(self.min_npc_level, int(combat_lvl * self.max_npc_level_ratio))

        try:
            candidates = []
            for npc in npcs.query().action('Attack').within(self.search_r).results():
                if npc.name is None:
                    continue
                if npc.name.lower() in ('duck', 'rat', 'cow', 'chicken'):
                    continue
                if npc.level < self.min_npc_level:
                    continue
                if npc.level > max_npc_lvl:
                    continue
                if self.target_names and npc.name.lower() not in self.target_names:
                    continue
                if npc.in_combat and npc.targets_another_player() and not npc.targets_me():
                    continue
                if self.skip_until.get(npc.index, 0) > now:
                    continue
                candidates.append(npc)

            if not candidates:
                self.no_target_streak += 1
                if self.verbose_location and self.no_target_streak % 50 == 0:
                    log('no targets found (min:' + str(self.min_npc_level) + ', max:' + str(max_npc_lvl) + ', search_r:' + str(self.search_r) + ') at ' + str(game.tile()))
                return None

            self.no_target_streak = 0

            if self.prefer_high:
                candidates.sort(key=lambda n: (-n.level, 0 if n.targets_me() else 1, 0 if not n.in_combat else 1, n.distance()))
            else:
                candidates.sort(key=lambda n: (0 if n.targets_me() else 1, 0 if not n.in_combat else 1, n.distance()))

            c = candidates[0]
            self.target = c.index
            if self.verbose_location:
                log('target acquired: ' + str(c.name) + ' lvl ' + str(c.level) + ' at ' + str(c.tile()))
            return c

        except Exception as e:
            if self.verbose_location:
                log('find_target error: ' + str(e))
            return None

    def do_fight(self):
        """Handle combat: attack target, track XP, handle death/kill."""
        if not game.in_combat():
            # Combat ended (target died or we stopped) - kills are handled in "target invalid during combat"
            if self.verbose_location:
                log('do_fight: not in combat, target=' + str(self.target) + ', in_combat=' + str(game.in_combat()))
            # Not in combat but have target -> initiate attack
            if self.target is not None:
                yield from self._initiate_attack()
            else:
                self.target = None
                self.fight_start_tick = 0
            return

        # In combat - validate target
        if self.target is None:
            if self.verbose_location:
                log('in combat but no target - clearing')
            return

        t = npcs.get(self.target)
        if t is None or not t.valid():
            if self.verbose_location:
                log('target invalid during combat (index=' + str(self.target) + ', in_combat=' + str(game.in_combat()) + ') - treating as kill')
            # Target died during combat - this is a KILL
            cur_xp = sum([skills.xp(s) for s in COMBAT_SKILLS])
            if cur_xp > self.last_xp:
                self.kills += 1
                self.last_xp = cur_xp
                self.last_xp_time = game.tick()
                tile = game.tile()
                if tile:
                    self.last_success_tile = tile
                    if self.verbose_location:
                        log('kill #' + str(self.kills) + ' at ' + str(tile) + ' (XP: ' + str(cur_xp) + ')')
            else:
                if self.verbose_location:
                    log('target lost without XP')
            self.target = None
            self.fight_start_tick = 0
            return

        # Track combat duration
        if self.fight_start_tick == 0:
            self.fight_start_tick = game.tick()
            if self.verbose_location:
                log('combat started vs ' + str(t.name) + ' lvl ' + str(t.level))

        # Track XP during combat
        cur_xp = sum([skills.xp(s) for s in COMBAT_SKILLS])
        if cur_xp > self.last_xp:
            if self.verbose_location:
                log('XP gain: ' + str(cur_xp - self.last_xp) + ' (total: ' + str(cur_xp) + ')')
            self.last_xp = cur_xp
            self.last_xp_time = game.tick()
            tile = game.tile()
            if tile:
                self.last_success_tile = tile

        # Max fight time exceeded
        if game.tick() - self.fight_start_tick > self.max_fight_ticks:
            if self.verbose_location:
                log('max fight time exceeded, giving up on ' + str(t.name))
            self.skip_until[self.target] = game.tick() + 60
            self.target = None
            self.fight_start_tick = 0
            return

    def _initiate_attack(self):
        """Called when not in combat but we have a target to attack."""
        npc = self.find_target()
        if npc is None:
            self.target = None
            self.fight_start_tick = 0
            return
        if self.verbose_location:
            log('attempting attack on ' + str(npc.name) + ' lvl ' + str(npc.level) + ' at ' + str(npc.tile()) + ' dist=' + str(npc.distance()) + ' valid=' + str(npc.valid()))
        # Walk to melee range if needed (distance > 1)
        if npc.distance() > 1:
            if self.verbose_location:
                log('walking to melee range of ' + str(npc.name))
            try:
                yield from traversal.walk_resilient(npc.tile(), radius=1, timeout_ms=10000, log=lambda m: None)
            except Exception:
                pass
        if npc.interact('Attack'):
            log('attacking ' + str(npc.name) + ' lvl ' + str(npc.level))
            self.target = npc.index
            self.last_xp = sum([skills.xp(s) for s in COMBAT_SKILLS])
            # Wait for combat to actually start
            start = game.tick()
            while not game.in_combat() and game.tick() - start < 20:
                yield from execution.delay_ticks(1)
            if game.in_combat():
                self.fight_start_tick = game.tick()
                if self.verbose_location:
                    log('combat started vs ' + str(npc.name))
            else:
                if self.verbose_location:
                    log('attack queued but combat not started, will retry')
        else:
            if self.verbose_location:
                log('attack failed on ' + str(npc.name) + ' - adding to skip')
            self.skip_until[npc.index] = game.tick() + 60
            yield from execution.delay_ticks(1)
            self.target = None
            self.fight_start_tick = 0
            return

    def wander(self):
        """Non-combat logic: scout for spots, walk to them, or random wander."""
        if self.verbose_location:
            log('wander() called - combat=' + str(game.in_combat()) + ', moving=' + str(game.moving()) + ', target=' + str(self.target))

        if game.in_combat() or game.moving():
            yield from execution.delay_ticks(1)
            return

        here = game.tile()
        if here is None:
            return

        now = game.tick()

        # Abysmal XP check - force rescout
        if now - self.last_xp_time > self.abysmal_ticks and self.last_xp_time > 0:
            self.no_target_streak = max(self.no_target_streak, 10)
            self.last_xp_time = now
            if self.verbose_location:
                log('abysmal XP - forcing rescout')

        # SCOUT: find best combat spot
        best_spot = self.find_best_combat_spot()
        if best_spot:
            if self.verbose_location:
                log('scout found spot: ' + str(best_spot) + ' - walking there')
            try:
                yield from traversal.walk_resilient(best_spot, radius=4, timeout_ms=120000, log=lambda m: None)
                if self.verbose_location:
                    log('arrived at scout spot')
            except Exception as e:
                if self.verbose_location:
                    log('walk to scout spot failed: ' + str(e))
            yield from execution.delay_ticks(2)
            return

        # No good spot found - RANDOM WANDER
        if self.verbose_location:
            log('no scout spot found - random wander')
        import random
        wx = random.randint(-self.wander_r, self.wander_r)
        wz = random.randint(-self.wander_r, self.wander_r)
        dest = Tile(here.x + wx, here.z + wz, here.level)
        if self.verbose_location:
            log('wandering from ' + str(here) + ' to ' + str(dest))
        try:
            yield from traversal.walk_resilient(dest, radius=3, timeout_ms=90000, log=lambda m: None)
        except Exception:
            pass
        yield from execution.delay_ticks(2)

    def find_best_combat_spot(self):
        """Scout for best combat spot within scout_radius."""
        here = game.tile()
        if here is None:
            return None

        try:
            npcs_in_range = list(npcs.query().action('Attack').within(self.scout_r).results())
            if self.verbose_location:
                log('scout: ' + str(len(npcs_in_range)) + ' attackable NPCs in ' + str(self.scout_r) + ' tiles at ' + str(here))
            if not npcs_in_range:
                return None

            combat_lvl = self._combat_level()
            max_npc_lvl = max(self.min_npc_level, int(combat_lvl * self.max_npc_level_ratio))
            valid_npcs = []

            for npc in npcs_in_range:
                if npc.name is None:
                    continue
                if npc.name.lower() in ('duck', 'rat', 'cow', 'chicken'):
                    continue
                if npc.level < self.min_npc_level:
                    continue
                if npc.level > max_npc_lvl:
                    continue
                if self.skip_until.get(npc.index, 0) > game.tick():
                    continue
                valid_npcs.append(npc)

            if self.verbose_location:
                log('scout: ' + str(len(valid_npcs)) + ' valid NPCs (min_lvl:' + str(self.min_npc_level) + ', max_lvl:' + str(max_npc_lvl) + ')')

            if len(valid_npcs) < self.min_npcs_per_spot:
                if self.verbose_location:
                    log('scout: not enough valid NPCs (' + str(len(valid_npcs)) + ' < ' + str(self.min_npcs_per_spot) + ')')
                return None

            # Group by 20x20 grid cells
            cells = {}
            for npc in valid_npcs:
                tile = npc.tile()
                gx = (tile.x // 20) * 20
                gz = (tile.z // 20) * 20
                key = (gx, gz)
                if key not in cells:
                    cells[key] = []
                cells[key].append(npc)

            if self.verbose_location:
                log('scout: ' + str(len(cells)) + ' grid cells with NPCs')

            # Score each cell
            best_score = 0
            best_center = None

            for key in cells:
                cell_npcs = cells[key]
                gx, gz = key
                if len(cell_npcs) < self.min_npcs_per_spot:
                    continue

                # Player penalty: count NPCs in combat with other players
                player_penalty_count = 0
                for npc in cell_npcs:
                    if npc.in_combat and npc.targets_another_player() and not npc.targets_me():
                        player_penalty_count += 1

                total_level = 0
                for n in cell_npcs:
                    total_level += n.level
                avg_level = total_level / len(cell_npcs)
                score = len(cell_npcs) * avg_level - player_penalty_count * self.player_penalty

                if score > best_score and score >= self.spot_score_min:
                    best_score = score
                    best_center = Tile(gx + 10, gz + 10, here.level)

            if best_center and self.verbose_location:
                cell_key = (best_center.x // 20 * 20, best_center.z // 20 * 20)
                cell_npc_count = len(cells.get(cell_key, []))
                log('spot score: ' + str(int(best_score)) + ' at ' + str(best_center) + ' (npcs in cell: ' + str(cell_npc_count) + ')')

            if best_center:
                return best_center
            else:
                if self.verbose_location:
                    log('scout: no cell met score threshold (' + str(self.spot_score_min) + ')')
                return None

        except Exception as e:
            if self.verbose_location:
                log('scout error: ' + str(e))
            return None

    def on_skill_level(self, e):
        if e.name in ('attack', 'strength', 'defence', 'hitpoints'):
            log('advanced ' + str(e.name) + ' to ' + str(e.level))

    def on_progress_report(self):
        xp = sum([skills.xp(s) for s in COMBAT_SKILLS]) - self.xp_start
        here = game.tile()
        loc = str(here) if here else 'unknown'
        return {
            'Kills': self.kills,
            'Combat XP': xp,
            'HP%': int(skills.hp_fraction() * 100),
            'Attack': skills.level('attack'),
            'Strength': skills.level('strength'),
            'Defence': skills.level('defence'),
            'Location': loc
        }


BOT = define_bot(
    name='WanderPunch',
    create=WanderPunch,
    description='Scouts for combat spots, fights to death, respawns, repeats',
    category='Combat',
    settings_schema=SETTINGS,
)