"""podd's one command line:

    python3 -m podd run       the daemon (systemd/podd.service runs this; never two at once: each deletes every
                              pod pod.sh records when it starts)
    python3 -m podd status    the pods and every queue's stages, with state and age
"""
import sys

COMMANDS = ("run", "status")


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) != 1 or arguments[0] not in COMMANDS:
        raise SystemExit(f"podd: (╯°□°)╯ expected one of {COMMANDS}, got {arguments}\n{__doc__}")
    if arguments[0] == "run":
        from podd import daemon
        daemon.run_forever()
    else:
        from podd import status
        status.print_status()


if __name__ == "__main__":
    main()
