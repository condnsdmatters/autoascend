import numpy as np
from collections import namedtuple
import gym
import nle
from nle.nethack import actions as A
import nle.nethack as nh
from glyph import SS, MON, C, ALL
from itertools import chain
import operator
from functools import partial
from pprint import pprint
import time


BLStats = namedtuple('BLStats', 'x y strength_percentage strength dexterity constitution intelligence wisdom charisma score hitpoints max_hitpoints depth gold energy max_energy armor_class monster_level experience_level experience_points time hunger_state carrying_capacity dungeon_number level_number')


class G: # Glyphs
    FLOOR : ['.'] = {SS.S_room, SS.S_ndoor, SS.S_darkroom}
    STONE : [' '] = {SS.S_stone}
    WALL : ['|', '-'] = {SS.S_vwall, SS.S_hwall, SS.S_tlcorn, SS.S_trcorn, SS.S_blcorn, SS.S_brcorn,
                         SS.S_crwall, SS.S_tuwall, SS.S_tdwall, SS.S_tlwall, SS.S_trwall}
    CORRIDOR : ['#'] = {SS.S_corr}
    STAIR_UP : ['<'] = {SS.S_upstair}
    STAIR_DOWN : ['>'] = {SS.S_dnstair}

    DOOR_CLOSED : ['+'] = {SS.S_vcdoor, SS.S_hcdoor}
    DOOR_OPENED : ['-', '|'] = {SS.S_vodoor, SS.S_hodoor}
    DOORS = set.union(DOOR_CLOSED, DOOR_OPENED)


    MONS = set(MON.ALL_MONS)
    PETS = set(MON.ALL_PETS)


    DICT = {k: v for k, v in locals().items() if not k.startswith('_')}

    @classmethod
    def assert_map(cls, glyphs, chars):
        for glyph, char in zip(glyphs.reshape(-1), chars.reshape(-1)):
            char = bytes([char]).decode()
            for k, v in cls.__annotations__.items():
                assert glyph not in cls.DICT[k] or char in v, f'{k} {v} {glyph} {char}'

G.INV_DICT = {i: [k for k, v in G.DICT.items() if i in v]
              for i in set.union(*map(set, G.DICT.values()))}


class AgentFinished(Exception):
    pass

class AgentPanic(Exception):
    pass

class AgentChangeStrategy(Exception):
    pass

class Agent:
    def __init__(self, env, seed=0, verbose=False):
        self.env = env
        self.verbose = verbose
        self.rng = np.random.RandomState(seed)
        self.all_panics = []

        self.last_observation = env.reset()
        self.score = 0
        self.update_map(is_first=True)

    def step(self, action):
        obs, reward, done, info = self.env.step(action)

        self.last_observation = obs
        self.score += reward
        if done:
            raise AgentFinished()

        self.update_map(is_first=False)

        return obs, reward, done, info

    def update_map(self, is_first):
        obs = self.last_observation

        self.blstats = BLStats(*obs['blstats'])
        self.glyphs = obs['glyphs']
        self.message = bytes(obs['message']).decode()

        if b'--More--' in bytes(obs['tty_chars'].reshape(-1)):
            self.step(A.Command.ESC)
            return

        if b'[yn]' in bytes(obs['tty_chars'].reshape(-1)):
            self.step(A.CompassDirection.NW) # y
            return

        self.update_level()

        self.on_update(is_first)


    ######## TRIVIAL ACTIONS

    def calc_direction(self, from_y, from_x, to_y, to_x):
        assert abs(from_y - to_y) <= 1 and abs(from_x - to_x) <= 1

        ret = ''
        if to_y == from_y + 1: ret += 's'
        if to_y == from_y - 1: ret += 'n'
        if to_x == from_x + 1: ret += 'e'
        if to_x == from_x - 1: ret += 'w'
        if ret == '': ret = '.'

        return ret

    def open_door(self, y, x=None):
        assert self.glyphs[y, x] in G.DOOR_CLOSED
        self.direction(y, x)
        return self.glyphs[y, x] not in G.DOOR_CLOSED

    def fight(self, y, x=None):
        assert self.glyphs[y, x] in G.MONS
        self.direction(y, x)
        return True

    def kick(self, y, x=None):
        self.step(A.Command.KICK)
        self.move(y, x)
        return self.blstats.y == y and self.blstats.x == x

    def search(self):
        self.step(A.Command.SEARCH)
        self.current_level().search_count[self.blstats.y, self.blstats.x] += 1
        return True

    def direction(self, y, x=None):
        if x is not None:
            dir = self.calc_direction(self.blstats.y, self.blstats.x, y, x)
        else:
            dir = y

        action = {
            'n': A.CompassDirection.N, 's': A.CompassDirection.S,
            'e': A.CompassDirection.E, 'w': A.CompassDirection.W,
            'ne': A.CompassDirection.NE, 'se': A.CompassDirection.SE,
            'nw': A.CompassDirection.NW, 'sw': A.CompassDirection.SW,
            '>': A.MiscDirection.DOWN, '<': A.MiscDirection.UP
        }[dir]

        self.step(action)
        return True

    def move(self, y, x=None):
        if x is not None:
            dir = self.calc_direction(self.blstats.y, self.blstats.x, y, x)
        else:
            dir = y

        expected_y = self.blstats.y + ('s' in dir) - ('n' in dir)
        expected_x = self.blstats.x + ('e' in dir) - ('w' in dir)

        self.direction(dir)

        if self.blstats.y != expected_y or self.blstats.x != expected_x:
            raise AgentPanic(f'agent position do not match after "move": '
                             f'expected ({expected_y}, {expected_x}), got ({self.blstats.y}, {self.blstats.x})')

    ########

    def neighbors(self, y, x, shuffle=True, diagonal=True):
        ret = []
        for dy in [-1, 0, 1]:
            for dx in [-1, 0, 1]:
                if dy == 0 and dx == 0:
                    continue
                if not diagonal and abs(dy) + abs(dx) > 1:
                    continue
                ny = y + dy
                nx = x + dx
                if 0 <= ny < C.SIZE_Y and 0 <= nx < C.SIZE_X:
                    ret.append((ny, nx))

        if shuffle:
            self.rng.shuffle(ret)

        return ret

    class Level:
        def __init__(self):
            self.walkable = np.zeros((C.SIZE_Y, C.SIZE_X), bool)
            self.seen = np.zeros((C.SIZE_Y, C.SIZE_X), bool)
            self.objects = np.zeros((C.SIZE_Y, C.SIZE_X), np.int16)
            self.objects[:] = -1
            self.search_count = np.zeros((C.SIZE_Y, C.SIZE_X), np.int32)

    levels = {}

    def current_level(self):
        key = (self.blstats.dungeon_number, self.blstats.level_number)
        if key not in self.levels:
            self.levels[key] = self.Level()
        return self.levels[key]

    def update_level(self):
        level = self.current_level()

        for y in range(C.SIZE_Y):
            for x in range(C.SIZE_X):
                if any(map(lambda s: operator.contains(s, self.glyphs[y, x]),
                           [G.FLOOR, G.CORRIDOR, G.STAIR_UP, G.STAIR_DOWN, G.DOOR_OPENED])):
                    level.walkable[y, x] = True
                    level.seen[y, x] = True
                    level.objects[y, x] = self.glyphs[y, x]
                elif any(map(lambda s: operator.contains(s, self.glyphs[y, x]),
                             [G.WALL, G.DOOR_CLOSED])):
                    level.seen[y, x] = True
                    level.objects[y, x] = self.glyphs[y, x]
                elif any(map(lambda s: operator.contains(s, self.glyphs[y, x]),
                             [G.MONS, G.PETS])):
                    level.seen[y, x] = True
                    level.walkable[y, x] = True

        for y, x in self.neighbors(self.blstats.y, self.blstats.x):
            if self.glyphs[y, x] in G.STONE:
                level.seen[y, x] = True
                level.objects[y, x] = self.glyphs[y, x]

    def bfs(self, y=None, x=None):
        if y is None:
            y = self.blstats.y
        if x is None:
            x = self.blstats.x

        level = self.current_level()

        dis = np.zeros((C.SIZE_Y, C.SIZE_X), dtype=np.int16)
        dis[:] = -1
        dis[y, x] = 0

        buf = np.zeros((C.SIZE_Y * C.SIZE_X, 2), dtype=np.uint16)
        index = 0
        buf[index] = (y, x)
        size = 1
        while index < size:
            y, x = buf[index]
            index += 1

            # TODO: handle situations
            # dir: SE
            # @|
            # -.
            # TODO: debug diagonal moving into and from doors
            for py, px in self.neighbors(y, x):
                if (level.walkable[py, px] and
                    (abs(py - y) + abs(px - x) <= 1 or
                     (level.objects[py, px] not in G.DOORS and
                      level.objects[y, x] not in G.DOORS))):
                    if dis[py, px] == -1:
                        dis[py, px] = dis[y, x] + 1
                        buf[size] = (py, px)
                        size += 1

        return dis

    def path(self, from_y, from_x, to_y, to_x, dis=None):
        if from_y == to_y and from_x == to_x:
            return [(to_y, to_x)]

        if dis is None:
            dis = self.bfs(from_y, from_x)

        assert dis[to_y, to_x] != -1

        cur_y, cur_x = to_y, to_x
        path_rev = [(cur_y, cur_x)]
        while cur_y != from_y or cur_x != from_x:
            for y, x in self.neighbors(cur_y, cur_x):
                if dis[y, x] < dis[cur_y, cur_x] and dis[y, x] >= 0:
                    path_rev.append((y, x))
                    cur_y, cur_x = y, x
                    break
            else:
                assert 0

        assert dis[cur_y, cur_x] == 0 and from_y == cur_y and from_x == cur_x
        path = path_rev[::-1]
        assert path[0] == (from_y, from_x) and path[-1] == (to_y, to_x)
        return path


    def is_any_mon_on_map(self):
        for y in range(C.SIZE_Y):
            for x in range(C.SIZE_X):
                if y != self.blstats.y or x != self.blstats.x:
                    if self.glyphs[y, x] in G.MONS:
                        return True
        return False

    def on_update(self, is_first):
        if is_first:
            return

        if self.is_any_mon_on_map():
            raise AgentChangeStrategy()


    ######## STRATEGIES ACTIONS

    def fight1(self):
        dis = self.bfs()
        closest = None

        # TODO: iter by distance
        for y in range(C.SIZE_Y):
            for x in range(C.SIZE_X):
                if y != self.blstats.y or x != self.blstats.x:
                    if self.glyphs[y, x] in G.MONS:
                        if dis[y, x] != -1 and (closest is None or dis[y, x] < dis[closest]):
                            closest = (y, x)

        if closest is None:
            return False

        y, x = closest
        path = self.path(self.blstats.y, self.blstats.x, y, x)[1:] # TODO: allow diagonal fight from doors

        if len(path) == 1:
            self.fight(*path[0])
        else:
            self.move(*path[0])

    def explore1(self):
        for py, px in self.neighbors(self.blstats.y, self.blstats.x, diagonal=False):
            if self.glyphs[py, px] in G.DOOR_CLOSED:
                if not self.open_door(py, px):
                    while self.glyphs[py, px] in G.DOOR_CLOSED:
                        self.kick(py, px)
                break

        level = self.current_level()
        to_explore = np.zeros((C.SIZE_Y, C.SIZE_X), dtype=bool)
        dis = self.bfs()
        for y in range(C.SIZE_Y):
            for x in range(C.SIZE_X):
                if dis[y, x] != -1:
                    for py, px in self.neighbors(y, x):
                        if not level.seen[py, px] and self.glyphs[py, px] in G.STONE:
                            to_explore[y, x] = True
                            break
                    for py, px in self.neighbors(y, x, diagonal=False):
                        if self.glyphs[py, px] in G.DOOR_CLOSED:
                            to_explore[y, x] = True
                            break

        nonzero_y, nonzero_x = \
                (dis == (dis * (to_explore) - 1).astype(np.uint16).min() + 1).nonzero()
        nonzero = [(y, x) for y, x in zip(nonzero_y, nonzero_x) if to_explore[y, x]]
        if len(nonzero) == 0:
            return False

        nonzero_y, nonzero_x = zip(*nonzero)
        ty, tx = nonzero_y[0], nonzero_x[0]

        #for asd in to_explore:
        #    print(str(asd.astype(np.int8).tolist()).replace(',', '').replace(' ', '').replace('-1', 'x')[1:-1])
        del level


        path = self.path(self.blstats.y, self.blstats.x, ty, tx, dis=dis)
        for y, x in path[1:]:
            if not self.current_level().walkable[y, x]:
                return
            self.move(y, x)

    def search1(self):
        level = self.current_level()
        dis = self.bfs()

        prio = np.zeros((C.SIZE_Y, C.SIZE_X), np.float32)
        for y in range(C.SIZE_Y):
            for x in range(C.SIZE_X):
                if not level.walkable[y, x] or dis[y, x] == -1:
                    prio[y, x] = -np.inf
                else:
                    prio[y, x] = -20
                    prio[y, x] -= dis[y, x]
                    prio[y, x] -= level.search_count[y, x] ** 2 * 10
                    prio[y, x] += (level.objects[y, x] in G.CORRIDOR) * 15 + (level.objects[y, x] in G.DOORS) * 80
                    for py, px in self.neighbors(y, x, shuffle=False):
                        prio[y, x] += (level.objects[py, px] in G.STONE) * 40 + (level.objects[py, px] in G.WALL) * 20

        nonzero_y, nonzero_x = (prio == prio.max()).nonzero()
        assert len(nonzero_y) >= 0

        ty, tx = nonzero_y[0], nonzero_x[0]
        path = self.path(self.blstats.y, self.blstats.x, ty, tx, dis=dis)
        for y, x in path[1:]:
            if not self.current_level().walkable[y, x]:
                return
            self.move(y, x)
        self.search()

    def move_down(self):
        level = self.current_level()

        pos = None
        for y in range(C.SIZE_Y):
            for x in range(C.SIZE_X):
                if level.objects[y, x] in G.STAIR_DOWN:
                    pos = (y, x)
                    break
            else:
                continue
            break

        if pos is None:
            return False

        dis = self.bfs()
        if dis[pos] == -1:
            return False

        ty, tx = pos

        path = self.path(self.blstats.y, self.blstats.x, ty, tx, dis=dis)
        for y, x in path[1:]:
            if not self.current_level().walkable[y, x]:
                return
            self.move(y, x)

        self.direction('>')




    def select_strategy(self):
        if self.is_any_mon_on_map():
            if self.fight1() is not False:
                return

        if self.explore1() is not False:
            return

        if self.move_down() is not False:
            return

        if self.search1() is not False:
            return

        assert 0


    ####### MAIN

    def main(self):
        try:
            while 1:
                try:
                    self.select_strategy()
                except AgentPanic as e:
                    self.all_panics.append(e)
                    if self.verbose:
                        print(f'PANIC!!!! : {e}')
                except AgentChangeStrategy:
                    pass
        except AgentFinished:
            pass






class EnvWrapper:
    def __init__(self, env):
        self.env = env

    def reset(self):
        print('\n' * 100)
        obs = self.env.reset()
        self.score = 0
        self.render(obs)

        G.assert_map(obs['glyphs'], obs['chars'])

        blstats = BLStats(*obs['blstats'])
        assert obs['chars'][blstats.y, blstats.x] == ord('@')

        return obs

    def render(self, obs):
        print(bytes(obs['message']).decode())
        print()
        print(BLStats(*obs['blstats']))
        print('Score:', self.score)
        print('Steps:', self.env._steps)
        print('Turns:', self.env._turns)
        print('Seed :', self.env.get_seeds())
        print()
        for letter, text in zip(obs['inv_letters'], obs['inv_strs']):
            if (text != 0).any():
                print(chr(letter), '->', bytes(text).decode())
        print('-' * 20)
        self.env.render()
        print('-' * 20)
        print()

    def print_help(self):
        scene_glyphs = set(self.env.last_observation[0].reshape(-1))
        obj_classes = {getattr(nh, x): x for x in dir(nh) if x.endswith('_CLASS')}
        glyph_classes = sorted((getattr(nh, x), x) for x in dir(nh) if x.endswith('_OFF'))

        texts = []
        for i in range(nh.MAX_GLYPH):
            desc = ''
            if glyph_classes and i == glyph_classes[0][0]:
                cls = glyph_classes.pop(0)[1]

            if nh.glyph_is_monster(i):
                desc = f': "{nh.permonst(nh.glyph_to_mon(i)).mname}"'

            if nh.glyph_is_normal_object(i):
                obj = nh.objclass(nh.glyph_to_obj(i))
                appearance = nh.OBJ_DESCR(obj) or nh.OBJ_NAME(obj)
                oclass = ord(obj.oc_class)
                desc = f': {obj_classes[oclass]}: "{appearance}"'

            desc2 = 'Labels: '
            if i in G.INV_DICT:
                desc2 += ','.join(G.INV_DICT[i])

            if i in scene_glyphs:
                pos = (self.env.last_observation[0].reshape(-1) == i).nonzero()[0]
                count = len(pos)
                pos = pos[0]
                char = bytes([self.env.last_observation[1].reshape(-1)[pos]])
                texts.append((-count, f'{" " if i in G.INV_DICT else "U"} Glyph {i:4d} -> '
                                      f'Char: {char} Count: {count:4d} '
                                      f'Type: {cls.replace("_OFF",""):11s} {desc:30s} '
                                      f'{ALL.find(i) if ALL.find(i) is not None else "":20} '
                                      f'{desc2}'))
        for _, t in sorted(texts):
            print(t)

    def get_action(self):
        while 1:
            key = os.read(sys.stdin.fileno(), 3)
            if len(key) != 1:
                print('wrong key', key)
                continue
            key = key[0]
            if key == 63: # '?"
                self.print_help()
                continue
            elif key == 10:
                return None
            else:
                actions = [a for a in self.env._actions if int(a) == key]
                assert len(actions) < 2
                if len(actions) == 0:
                    print('wrong key', key)
                    continue

                action = actions[0]
                return action

    def step(self, agent_action):
        print()
        print('agent_action:', agent_action)
        print()

        action = self.get_action()
        if action is None:
            action = agent_action
        print('\n' * 10)
        print('action:', action)
        print()

        obs, reward, done, info = self.env.step(self.env._actions.index(action))
        self.score += reward
        self.render(obs)
        G.assert_map(obs['glyphs'], obs['chars'])
        return obs, reward, done, info


class EnvLimitWrapper:
    def __init__(self, env, step_limit):
        self.env = env
        self.step_limit = step_limit

    def reset(self):
        self.cur_step = 0
        self.last_turn = 0
        self.levels = set()
        return self.env.reset()

    def step(self, action):
        obs, reward, done, info = self.env.step(self.env._actions.index(action))
        blstats = BLStats(*obs['blstats'])
        self.levels.add((blstats.dungeon_number, blstats.level_number))
        self.cur_step += 1
        self.last_turn = max(self.last_turn, self.env._turns)
        if self.cur_step == self.step_limit + 1:
            return obs, reward, True, info
        elif self.cur_step > self.step_limit + 1:
            assert 0
        return obs, reward, done, info



if __name__ == '__main__':
    import sys, tty, os, termios

    if len(sys.argv) <= 1:
        from multiprocessing import Pool, Process, Queue
        from matplotlib import pyplot as plt
        import seaborn as sns
        sns.set()


        result_queue = Queue()
        def single_simulation(seed):
            start_time = time.time()
            env = EnvLimitWrapper(gym.make('NetHackChallenge-v0'), 10000)
            env.env.seed(seed, seed)
            agent = Agent(env, verbose=False)
            agent.main()
            end_time = time.time()
            result_queue.put({
                'score': agent.score,
                'steps': env.env._steps,
                'turns': env.last_turn,
                'duration': end_time - start_time,
                'level_num': len(agent.levels),
                'seed': seed,
            })


        start_time = time.time()

        plot_queue = Queue()
        def plot_thread_func():
            fig = plt.figure()
            plt.show(block=False)
            while 1:
                try:
                    res = plot_queue.get(block=False)
                except:
                    plt.pause(0.01)
                    continue

                fig.clear()
                spec = fig.add_gridspec(len(res), 2)
                for i, k in enumerate(sorted(res)):
                    ax = fig.add_subplot(spec[i, 0])
                    ax.set_title(k)
                    sns.histplot(res[k], kde=np.var(res[k]) > 1e-6, bins=len(res[k]) // 5 + 1, ax=ax)

                ax = fig.add_subplot(spec[:len(res) // 2, 1])
                sns.scatterplot(x='turns', y='steps', data=res, ax=ax)

                ax = fig.add_subplot(spec[len(res) // 2:, 1])
                sns.scatterplot(x='turns', y='score', data=res, ax=ax)

                plt.show(block=False)


        plt_process = Process(target=plot_thread_func)
        plt_process.start()

        all_res = {}
        count = 0
        simulation_processes = []
        for _ in range(16):
            simulation_processes.append(Process(target=single_simulation, args=(count,)))
            simulation_processes[-1].start()
            count += 1

        while True:
            simulation_processes = [p for p in simulation_processes if p.is_alive() or (p.close() and False)]
            single_res = result_queue.get()

            simulation_processes.append(Process(target=single_simulation, args=(count,)))
            simulation_processes[-1].start()
            count += 1

            if not all_res:
                all_res = {key: [] for key in single_res}
            assert all_res.keys() == single_res.keys()

            count += 1
            for k, v in single_res.items():
                all_res[k].append(v)


            plot_queue.put(all_res)


            total_duration = time.time() - start_time

            print(list(zip(all_res['seed'], all_res['score'], all_res['turns'], all_res['steps'])))
            print(f'count                         : {count}')
            print(f'time_per_simulation           : {np.mean(all_res["duration"])}')
            print(f'time_per_turn                 : {np.sum(all_res["duration"]) / np.sum(all_res["turns"])}')
            print(f'turns_per_second              : {np.sum(all_res["turns"]) / np.sum(all_res["duration"])}')
            print(f'turns_per_second(multithread) : {np.sum(all_res["turns"]) / total_duration}')
            print(f'score_mean                    : {np.mean(all_res["score"])}')
            print(f'score_median                  : {np.median(all_res["score"])}')
            print(f'score_05-95                   : {np.quantile(all_res["score"], 0.05)} '
                                                    f'{np.quantile(all_res["score"], 0.95)}')
            print(f'score_25-75                   : {np.quantile(all_res["score"], 0.25)} '
                                                    f'{np.quantile(all_res["score"], 0.75)}')
            print()

    else:
        old_settings = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())

        seed = int(sys.argv[1])

        try:
            env = EnvWrapper(gym.make('NetHackChallenge-v0'))
            env.env.seed(seed, seed)

            agent = Agent(env, verbose=True)
            agent.main()

        finally:
            os.system('stty sane')
