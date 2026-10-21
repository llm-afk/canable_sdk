"""CANable example: enumerate by default; send only with an explicit CAN ID."""
import argparse
import json
import time

import canable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial')
    parser.add_argument('--id', type=lambda text: int(text, 0), dest='can_id')
    parser.add_argument('--data', default='', help='hex bytes, e.g. 01020304')
    parser.add_argument('--rate', type=float, default=1000)
    parser.add_argument('--seconds', type=float, default=10)
    args = parser.parse_args()
    if args.can_id is None:
        print(json.dumps(canable.detect(), indent=2))
        return 0

    from canable.native import NativeBus
    with NativeBus(args.serial) as bus:
        bus.start_periodic(args.can_id, bytes.fromhex(args.data), args.rate, args.seconds)
        last = 0
        try:
            while True:
                while bus.recv() is not None:
                    pass  # This example counts frames; replace with application handling.
                stats = bus.stats()
                if stats['elapsed'] - last >= 1 or not stats['running']:
                    print(json.dumps(stats), flush=True)
                    last = stats['elapsed']
                if not stats['running']:
                    break
                time.sleep(.0001)
        except KeyboardInterrupt:
            pass
        finally:
            bus.stop()
        print('FINAL', json.dumps(bus.stats()))
        return int(bool(bus.stats()['errors'] or bus.stats()['pending']))


if __name__ == '__main__':
    raise SystemExit(main())
