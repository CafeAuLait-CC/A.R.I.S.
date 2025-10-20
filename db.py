import os, click
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from dotenv import load_dotenv
from models import Base, User, GPU

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///gpupool.sqlite3")
engine = create_engine(DATABASE_URL, future=True)

@click.group()
def cli(): pass

@cli.command("init")
def init_db():
    Base.metadata.create_all(engine)
    click.echo("DB initialized.")

@cli.command("add-gpus")
@click.option("--names", required=True)
def add_gpus(names):
    names = [n.strip() for n in names.split(",")]
    with Session(engine) as s:
        for n in names:
            if not s.scalar(select(GPU).where(GPU.name==n)):
                s.add(GPU(name=n))
        s.commit()
    click.echo(f"GPUs added: {', '.join(names)}")

@cli.command("add-user")
@click.option("--name", required=True)
@click.option("--quota", type=int, default=100)
def add_user(name, quota):
    with Session(engine) as s:
        if s.scalar(select(User).where(User.name==name)):
            click.echo("User exists"); return
        s.add(User(name=name, weekly_quota_minutes=quota*60))
        s.commit()
        click.echo(f"User {name} added with quota {quota}h.")

if __name__ == "__main__":
    cli()
