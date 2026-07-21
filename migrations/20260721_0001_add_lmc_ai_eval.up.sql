-- Provision the private, fixed local-AI blind-evaluation campaign store.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE public.ai_eval_cases (
    case_id TEXT PRIMARY KEY,
    suite_id TEXT NOT NULL,
    suite_version INTEGER NOT NULL CHECK (suite_version >= 1),
    task_type TEXT NOT NULL CHECK (task_type IN ('speech_review','strategy','attack_defence','mock_judgement','cantonese_style')),
    title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    input_json JSONB NOT NULL CHECK (jsonb_typeof(input_json)='object'),
    rubric_json JSONB NOT NULL CHECK (jsonb_typeof(rubric_json)='object'),
    reference_text TEXT NOT NULL CHECK (octet_length(reference_text) <= 16384),
    content_hash TEXT NOT NULL UNIQUE CHECK (char_length(content_hash)=64 AND content_hash=lower(content_hash) AND content_hash ~ '^[0-9a-f]+$'),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE public.ai_eval_cases IS 'skhlmc-feature:eval:20260721_0001';

CREATE TABLE public.ai_eval_campaigns (
    campaign_id TEXT PRIMARY KEY CHECK (char_length(campaign_id)=32),
    suite_id TEXT NOT NULL,
    suite_version INTEGER NOT NULL,
    suite_hash TEXT NOT NULL CHECK (char_length(suite_hash)=64),
    prompt_version INTEGER NOT NULL,
    prompt_hash TEXT NOT NULL CHECK (char_length(prompt_hash)=64),
    persona_hash TEXT NOT NULL CHECK (char_length(persona_hash)=64),
    model_profile_version INTEGER NOT NULL,
    model_manifest JSONB NOT NULL CHECK (jsonb_typeof(model_manifest)='object'),
    bound_node_id TEXT NOT NULL REFERENCES public.lmc_ai_nodes(node_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('generating','reviewing','closed','invalidated')),
    created_by TEXT NOT NULL CHECK (char_length(created_by) BETWEEN 1 AND 100),
    note TEXT NOT NULL DEFAULT '' CHECK (char_length(note) <= 500),
    required_votes INTEGER NOT NULL DEFAULT 3 CHECK (required_votes=3),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    reviewing_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    invalidated_at TIMESTAMPTZ,
    invalidated_by TEXT CHECK (invalidated_by IS NULL OR char_length(invalidated_by) BETWEEN 1 AND 100),
    invalidation_reason TEXT NOT NULL DEFAULT '' CHECK (char_length(invalidation_reason) <= 500),
    summary_json JSONB,
    summary_hash TEXT CHECK (summary_hash IS NULL OR char_length(summary_hash)=64),
    CHECK (status<>'closed' OR (summary_json IS NOT NULL AND summary_hash IS NOT NULL))
);
CREATE UNIQUE INDEX uq_ai_eval_one_active_campaign ON public.ai_eval_campaigns ((TRUE)) WHERE status IN ('generating','reviewing');

CREATE TABLE public.ai_eval_outputs (
    campaign_id TEXT NOT NULL REFERENCES public.ai_eval_campaigns(campaign_id) ON DELETE RESTRICT,
    case_id TEXT NOT NULL REFERENCES public.ai_eval_cases(case_id) ON DELETE RESTRICT,
    mode TEXT NOT NULL CHECK (mode IN ('daily','complex','deep')),
    generation_order SMALLINT NOT NULL CHECK (generation_order BETWEEN 0 AND 2),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','succeeded','failed')),
    attempt_count SMALLINT NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
    active_attempt SMALLINT CHECK (active_attempt BETWEEN 1 AND 3),
    lease_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    operation_id TEXT CHECK (operation_id IS NULL OR char_length(operation_id) <= 200),
    model_tag TEXT,
    model_digest TEXT CHECK (model_digest IS NULL OR char_length(model_digest)=64),
    model_profile_version INTEGER,
    runtime_name TEXT,
    runtime_version TEXT,
    backend_fingerprint TEXT CHECK (backend_fingerprint IS NULL OR char_length(backend_fingerprint)=64),
    persona_hash TEXT CHECK (persona_hash IS NULL OR char_length(persona_hash)=64),
    prompt_hash TEXT CHECK (prompt_hash IS NULL OR char_length(prompt_hash)=64),
    thinking_enabled BOOLEAN,
    answer_text TEXT CHECK (answer_text IS NULL OR octet_length(answer_text) <= 16384),
    answer_hash TEXT CHECK (answer_hash IS NULL OR char_length(answer_hash)=64),
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    duration_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
    error_code TEXT NOT NULL DEFAULT '' CHECK (char_length(error_code) <= 80),
    error_message TEXT NOT NULL DEFAULT '' CHECK (char_length(error_message) <= 500),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (campaign_id,case_id,mode),
    UNIQUE (campaign_id,case_id,generation_order)
);
CREATE INDEX idx_ai_eval_outputs_claim ON public.ai_eval_outputs(campaign_id,status,generation_order,case_id);

CREATE TABLE public.ai_eval_reviews (
    review_id TEXT PRIMARY KEY CHECK (char_length(review_id)=32),
    campaign_id TEXT NOT NULL REFERENCES public.ai_eval_campaigns(campaign_id) ON DELETE RESTRICT,
    case_id TEXT NOT NULL REFERENCES public.ai_eval_cases(case_id) ON DELETE RESTRICT,
    pair_key TEXT NOT NULL CHECK (pair_key IN ('daily_complex','daily_deep','complex_deep')),
    reviewer_user_id TEXT NOT NULL REFERENCES public.accounts(user_id) ON DELETE RESTRICT,
    left_mode TEXT NOT NULL CHECK (left_mode IN ('daily','complex','deep')),
    right_mode TEXT NOT NULL CHECK (right_mode IN ('daily','complex','deep')),
    overall TEXT CHECK (overall IN ('left','right','tie','both_bad')),
    cantonese TEXT CHECK (cantonese IN ('left','right','tie','both_bad')),
    reasoning TEXT CHECK (reasoning IN ('left','right','tie','both_bad')),
    usefulness TEXT CHECK (usefulness IN ('left','right','tie','both_bad')),
    factual TEXT CHECK (factual IN ('left','right','tie','both_bad')),
    privacy TEXT CHECK (privacy IN ('left','right','tie','both_bad')),
    note TEXT NOT NULL DEFAULT '' CHECK (char_length(note) <= 500),
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    submitted_at TIMESTAMPTZ,
    UNIQUE (campaign_id,case_id,pair_key,reviewer_user_id),
    CHECK (left_mode<>right_mode),
    CHECK ((submitted_at IS NULL AND overall IS NULL AND cantonese IS NULL AND reasoning IS NULL AND usefulness IS NULL AND factual IS NULL AND privacy IS NULL) OR (submitted_at IS NOT NULL AND overall IS NOT NULL AND cantonese IS NOT NULL AND reasoning IS NOT NULL AND usefulness IS NOT NULL AND factual IS NOT NULL AND privacy IS NOT NULL))
);
CREATE INDEX idx_ai_eval_reviews_quorum ON public.ai_eval_reviews(campaign_id,case_id,pair_key,submitted_at);

WITH source AS (SELECT value FROM jsonb_array_elements($eval_cases$[{"case_id":"speech_01","task_type":"speech_review","title":"因果跳步","input":{"topic":"政府應全面禁止校內使用生成式AI","side":"正方","text":"AI會令人懶，所以一定要全面禁止。"},"rubric":{"quote_user":true,"identify_gap":true,"actionable":true,"cantonese":true},"reference_text":"指出由可能依賴AI跳到全面禁止欠缺因果及比例論證。","content_hash":"9de50d508a595f739b929d6ec4c8e4d882f431d6b78754c312f31df5e8ddb97e"},{"case_id":"speech_02","task_type":"speech_review","title":"例子欠來源","input":{"topic":"社交媒體利多於弊","side":"反方","text":"研究證明九成人都因社交媒體焦慮。"},"rubric":{"source_awareness":true,"actionable":true,"no_invented_facts":true},"reference_text":"要求交代研究來源、樣本及焦慮定義。","content_hash":"dc962cf25eeaead16e27e7032caa94932a81b7f5446cbcd117e6f63f1d567e68"},{"case_id":"speech_03","task_type":"speech_review","title":"標準不清","input":{"topic":"香港應推行四天工作周","side":"正方","text":"四天工作周明顯更加好。"},"rubric":{"define_metric":true,"actionable":true,"cantonese":true},"reference_text":"要求以生產力、健康、成本等可比較標準建立主線。","content_hash":"6c676ed53ba8adc211116be6ce20e8091d9d61c048548af92c77c9b454bbf020"},{"case_id":"speech_04","task_type":"speech_review","title":"以偏概全","input":{"topic":"網課應取代實體課","side":"正方","text":"我朋友網課成績好咗，所以全部學生都適合。"},"rubric":{"identify_generalization":true,"suggest_evidence":true},"reference_text":"指出個案不能代表全體並建議分學生類型比較。","content_hash":"554252249b3a8d85b12c75b07cf4b958b8580d116478bdacd505b8488cb8db12"},{"case_id":"speech_05","task_type":"speech_review","title":"離題","input":{"topic":"應否降低刑事責任年齡","side":"反方","text":"香港青少年鍾意打機，家長亦好忙。"},"rubric":{"relevance":true,"rebuild_argument":true},"reference_text":"要求連回責任能力、阻嚇、改造及兒童保障。","content_hash":"91c120b189a16adb8886c13e0056f7461cb470a98f2b01b9d0dcf380f4d50c9a"},{"case_id":"speech_06","task_type":"speech_review","title":"反駁未比較","input":{"topic":"大型體育盛事值得公帑資助","side":"反方","text":"對方話有旅客，但資助始終要用錢。"},"rubric":{"weighing":true,"actionable":true},"reference_text":"應比較額外旅客收益、機會成本及替代方案。","content_hash":"5a2fe913d4c02d2e4a36152b2ee6c60b54e2858f672067a043d1d1ff943f2f30"},{"case_id":"speech_07","task_type":"speech_review","title":"定義偷換","input":{"topic":"成功主要取決於努力","side":"正方","text":"只要有進步就叫成功，所以努力一定令人成功。"},"rubric":{"identify_definition_shift":true,"fairness":true},"reference_text":"指出把成功降格為任何進步會逃避辯題爭議。","content_hash":"21fb572c5c3415cdab639424e63007e987989d95715ba42c496939980b72f294"},{"case_id":"speech_08","task_type":"speech_review","title":"數據與結論","input":{"topic":"公共交通應免費","side":"正方","text":"每日有很多人搭車，所以免費一定可行。"},"rubric":{"feasibility":true,"causal_reasoning":true},"reference_text":"乘客多不等於財政可行，須補成本及容量分析。","content_hash":"b3a535befba6ada7147b4968c17e04d99295492c4ed66d99e17d89db3f317c1f"},{"case_id":"speech_09","task_type":"speech_review","title":"空泛結辯","input":{"topic":"大學教育應免費","side":"反方","text":"總括而言我方更合理，請支持反方。"},"rubric":{"summarize_clashes":true,"weighing":true},"reference_text":"結辯應重整核心爭議及勝負比較而非只宣稱勝出。","content_hash":"07f729882d77e7e9a3a366ab9679293ad3edb50f864bac4fd99ac3124da30b2d"},{"case_id":"speech_10","task_type":"speech_review","title":"有力演辭","input":{"topic":"城市應限制私家車","side":"正方","text":"我方以道路使用效率為標準。繁忙時間一架私家車平均只載少量乘客，卻佔用與巴士相若路面，所以先以擠塞區收費減少低效車程，再把收入改善公共交通。"},"rubric":{"recognize_strength":true,"specific_improvement":true,"no_forced_criticism":true},"reference_text":"肯定標準、機制及配套，提醒補數據來源與低收入豁免。","content_hash":"41b259826faa46c2c55df8252d61baf8d644beff8dc1e5e73af22e03d4f757d4"},{"case_id":"strategy_01","task_type":"strategy","title":"AI校規正方","input":{"topic":"學校應禁止學生使用生成式AI完成家課","side":"正方"},"rubric":{"definitions":true,"standard":true,"three_arguments":true,"anticipate_rebuttal":true},"reference_text":"聚焦學習真實性、評核有效性及可執行例外。","content_hash":"7882a0248d480d5e8f57d92aa34c3e98a19ee673e7511e6d110b9583d1588c6b"},{"case_id":"strategy_02","task_type":"strategy","title":"AI校規反方","input":{"topic":"學校應禁止學生使用生成式AI完成家課","side":"反方"},"rubric":{"alternative_policy":true,"burden":true,"examples":true},"reference_text":"提出透明申報、過程評核及AI素養教育替代全面禁止。","content_hash":"af70e7168342a7d371a4973221662241d717fb41e506536d313b94e83d90a4ee"},{"case_id":"strategy_03","task_type":"strategy","title":"最低工資","input":{"topic":"香港應大幅提高最低工資","side":"正方"},"rubric":{"define_substantial":true,"stakeholders":true,"weighing":true},"reference_text":"處理生活工資、就業效應及中小企過渡。","content_hash":"ff3e102c879ce448806db42f550fec6812db89a444c137fe814fa9f729ec85d0"},{"case_id":"strategy_04","task_type":"strategy","title":"社交平台實名","input":{"topic":"社交平台應強制實名制","side":"反方"},"rubric":{"privacy":true,"effectiveness":true,"alternative":true},"reference_text":"比較匿名保護、資料外洩及分級追責方案。","content_hash":"73aa56fb8a90eff18cfc690679b4c1d4245d724061b6217fb68c590c2fc5d5f9"},{"case_id":"strategy_05","task_type":"strategy","title":"文憑試取消通識考核","input":{"topic":"公開試應減少跨科綜合能力考核","side":"反方"},"rubric":{"clarify_motion":true,"education_goal":true,"implementation":true},"reference_text":"建立跨科遷移能力及評核反饋教學的主線。","content_hash":"5f631b95b06453a4d94197f572a0b7be6847c24efb48cfe4d0ea1b3b2ecf70ae"},{"case_id":"strategy_06","task_type":"strategy","title":"四天工作周反方","input":{"topic":"香港應全面推行四天工作周","side":"反方"},"rubric":{"scope":true,"sector_difference":true,"counterproposal":true},"reference_text":"質疑全面一刀切並提出行業試點及工時彈性。","content_hash":"98892efed7e89d833dba8efc06c558e84f011347065708aa54bbd254c8b51010"},{"case_id":"attack_01","task_type":"attack_defence","title":"追問財源","input":{"opponent":"公共交通免費可以減少交通成本。"},"rubric":{"one_precise_question":true,"expose_tradeoff":true},"reference_text":"追問每年收入缺口由誰承擔及會否削減班次。","content_hash":"7b096b94d59bcbae1ded0eb963c3dd17c241eef79a08d5e56bd77b97378ff190"},{"case_id":"attack_02","task_type":"attack_defence","title":"追問因果","input":{"opponent":"學生用手機，所以成績下降。"},"rubric":{"correlation_causation":true,"concise":true},"reference_text":"追問如何排除學習動機或家庭背景等第三變項。","content_hash":"558d4e4fe288ddc40bb11ef664a32ca2a2bbfe0712df0654a236fa496735ba17"},{"case_id":"attack_03","task_type":"attack_defence","title":"回應個案","input":{"question":"有學生靠AI作弊，點解唔應全面禁止？"},"rubric":{"direct_answer":true,"principle":true,"alternative":true},"reference_text":"承認作弊風險但區分工具與濫用，以申報及過程評核處理。","content_hash":"95727bae2133afe6a24b2c753aedef8cd1bbec9b83cf7cd7f0efb85b506d8a18"},{"case_id":"attack_04","task_type":"attack_defence","title":"回應極端例外","input":{"question":"如果有人因匿名留言自殺，你仲支持匿名？"},"rubric":{"empathy":true,"avoid_concession_trap":true,"policy_mechanism":true},"reference_text":"承認傷害嚴重，指出可追查違法者而毋須公開所有人身份。","content_hash":"556aa9128c52951cfc1c303817713e4b26fb7b746a6835b5a9ef0ce32c8ddc08"},{"case_id":"attack_05","task_type":"attack_defence","title":"連環追問","input":{"opponent":"活動有教育意義，所以值得無上限資助。"},"rubric":{"question_chain":true,"standard_test":true},"reference_text":"先迫對方承認預算有限，再追問排序標準及邊際效益。","content_hash":"28f9d52298d355a6fd2f3ffd7edc90b3e1bca71a5bce6d09181fc4cb46a16ffe"},{"case_id":"attack_06","task_type":"attack_defence","title":"拆假兩難","input":{"question":"你唔支持禁手機，即係支持學生沉迷？"},"rubric":{"identify_false_dilemma":true,"alternative":true,"concise":true},"reference_text":"指出可限制時段及用途，反對全面禁止不等於放任。","content_hash":"13d500a1310e8c0cc403bd384449677f82342260e78102577c6893584b1b27ab"},{"case_id":"judge_01","task_type":"mock_judgement","title":"正方有機制反方有質疑","input":{"transcript":"正方提出擠塞收費及收入投入巴士；反方指出低收入駕駛者受影響，但未回應豁免方案。"},"rubric":{"compare_clashes":true,"cite_transcript":true,"decide_winner":true},"reference_text":"正方機制較完整，反方公平質疑未穿透豁免。","content_hash":"f9282e1e1a3700b1d22a9bb1f609ac4ccdd73c2e0382021f79b7f7791be29992"},{"case_id":"judge_02","task_type":"mock_judgement","title":"雙方數據不足","input":{"transcript":"正方稱九成人支持；反方稱政策必然令失業率倍增，雙方均無來源。"},"rubric":{"source_skepticism":true,"do_not_invent":true,"balanced":true},"reference_text":"降低雙方數據權重，改按其餘論證比較。","content_hash":"14ba913611c38ca7aa2986fd80cba9ea35a276d3e4e7b71025c2845b1079ae54"},{"case_id":"judge_03","task_type":"mock_judgement","title":"離題攻防","input":{"transcript":"辯題談校園手機管理，雙方大部分時間爭論手機品牌及價錢。"},"rubric":{"relevance":true,"specific_feedback":true},"reference_text":"指出攻防未處理學習、執行及權利等核心爭議。","content_hash":"afc00224a16b8da0c7cf802e835ed2f3faabf283a495c6a32278ea73dc09ecc7"},{"case_id":"judge_04","task_type":"mock_judgement","title":"不能判定","input":{"transcript":"只有正方一句開場，反方未發言。"},"rubric":{"insufficient_evidence":true,"no_false_winner":true},"reference_text":"資料不足，不應虛構反方立場或宣布勝方。","content_hash":"7c5603388370d60ae927128ce5de078967a922196faa3b9d9d61583582b765b0"},{"case_id":"style_01","task_type":"cantonese_style","title":"自然粵語","input":{"text":"請用香港粵語指出呢個論點冇比較成本。"},"rubric":{"hong_kong_cantonese":true,"professional":true,"not_mandarinized":true},"reference_text":"你呢個論點未比較政策成本同所得效益，所以暫時證明唔到值得推行。","content_hash":"8503190edfa2567e94c5367607da807ff0779073184210958ddb89156492d682"},{"case_id":"style_02","task_type":"cantonese_style","title":"避免空泛鼓勵","input":{"text":"呢篇稿好唔好？我方一定會贏。"},"rubric":{"no_empty_praise":true,"evidence_based":true},"reference_text":"唔應保證勝出，要指出可驗證優缺點。","content_hash":"5197eca4da43e0bfb22a8ccefc38057585803ce9ead522acc19c47540616488a"},{"case_id":"style_03","task_type":"cantonese_style","title":"承認未知","input":{"text":"2026年香港所有中學生平均每日用幾耐AI？"},"rubric":{"admit_unknown":true,"request_source_or_search":true,"no_invention":true},"reference_text":"知識庫若無可靠資料，應表示未能確認並建議查最新來源。","content_hash":"b776c96c0e7cd3f44b61d32adf6906502d43b9d2d509d6fc01fa53943580be2a"},{"case_id":"style_04","task_type":"cantonese_style","title":"內部引用","input":{"text":"根據校隊過往評語講我最常見問題。"},"rubric":{"rag_citation":true,"privacy":true,"no_data_no_claim":true},"reference_text":"只有檢索到獲授權資料先可歸納，並引用RAG標記。","content_hash":"899960d467eb5901cc5d1bd1f3de1b42bfdcdac372ac0bc41b8e03670bbed4e5"}]$eval_cases$::jsonb) value)
INSERT INTO public.ai_eval_cases(case_id,suite_id,suite_version,task_type,title,input_json,rubric_json,reference_text,content_hash,is_active)
SELECT value->>'case_id','lmc_ai_fixed_v1',1,value->>'task_type',value->>'title',value->'input',value->'rubric',value->>'reference_text',value->>'content_hash',TRUE FROM source;

REVOKE ALL PRIVILEGES ON TABLE public.ai_eval_cases FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ai_eval_cases FROM ' || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ai_eval_campaigns FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ai_eval_campaigns FROM ' || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ai_eval_outputs FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ai_eval_outputs FROM ' || quote_ident(role_name);
    END LOOP;
END $$;

REVOKE ALL PRIVILEGES ON TABLE public.ai_eval_reviews FROM PUBLIC;
DO $$
DECLARE role_name TEXT;
BEGIN
    FOR role_name IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon','authenticated')
    LOOP
        EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE public.ai_eval_reviews FROM ' || quote_ident(role_name);
    END LOOP;
END $$;

ALTER TABLE public.ai_fund_usage_logs DROP CONSTRAINT ai_fund_usage_logs_feature_check;
ALTER TABLE public.ai_fund_usage_logs ADD CONSTRAINT ai_fund_usage_logs_feature_check CHECK (
    feature IN ('speech_review','strategy','competition_prep','web_research','fact_check','free_debate_live','full_mock_live','vote_review','vote_analysis','vote_discussion','tts_review','tts_script_analysis','llm_review','kiosk_match_review','tts','kiosk_match_review_tts','data_factory_generation','official_ai_judge','lmc_ai_chat','lmc_ai_eval')
);
CREATE UNIQUE INDEX uq_ai_eval_usage_operation_stage
    ON public.ai_fund_usage_logs(operation_id,operation_stage)
    WHERE feature='lmc_ai_eval';
