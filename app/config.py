from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    DATABASE_URL: str = f"sqlite:///{BASE_DIR / 'invest_ledger.db'}"
    API_V1_PREFIX: str = "/api/v1"
    PROJECT_NAME: str = "水果深加工招商台账后端服务"
    # 风险快照访问令牌，格式：姓名:角色:令牌，多个以英文逗号分隔；
    # 角色取值 director（局长/负责人）、analyst（分析师，可生成）、viewer（专员，只读）。
    # 生产环境必须通过环境变量 RISK_SNAPSHOT_TOKENS 覆盖默认演示令牌。
    RISK_SNAPSHOT_TOKENS: str = (
        "张局长:director:director-token-2026,"
        "李分析师:analyst:analyst-token-2026,"
        "王专员:viewer:viewer-token-2026"
    )

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
