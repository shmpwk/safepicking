#!/usr/bin/env python

import argparse
import json

import imgviz
import path

from agent import DqnAgent
from env import GraspWithIntentEnv


here = path.Path(__file__).abspath().parent


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--weight-dir", type=path.Path, help="weight dir", required=True
    )
    parser.add_argument("--nogui", action="store_true", help="no gui")
    args = parser.parse_args()

    env = GraspWithIntentEnv(gui=not args.nogui, retime=10)
    env.eval = True
    obs = env.reset()

    log_dir = args.weight_dir.parent.parent
    with open(log_dir / "hparams.json") as f:
        hparams = json.load(f)

    agent = DqnAgent(env=env, model=hparams["model"])
    agent.build(training=False)
    agent.load_weights(args.weight_dir)

    while True:
        for key in obs:
            obs[key] = obs[key][None, None, :]
        act_result = agent.act(
            step=-1, observation=obs, deterministic=True, env=env
        )

        if 0:
            imgviz.io.cv_imshow(agent.draw_act_summary(), "act_summary")
            imgviz.io.cv_waitkey(100)

        transition = env.step(act_result)
        print(
            f"action={act_result.action}, reward={transition.reward}, "
            f"terminal={transition.terminal}",
        )
        if transition.info["needs_reset"]:
            obs = env.reset()
        else:
            obs = transition.observation


if __name__ == "__main__":
    main()
