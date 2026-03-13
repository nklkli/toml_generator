from pprint import pprint
from config import parse_config


def main():
    root_config = parse_config()
    pprint(root_config)


if __name__ == "__main__":
    main()
