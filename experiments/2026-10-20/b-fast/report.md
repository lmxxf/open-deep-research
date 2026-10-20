# 一家50人的软件公司准备做内部文档问答助手，应该采用DeepSeek API还是本地部署Qwen？请比较第一版的实现路线、资料更新和权限控制，以及最容易踩的坑。


## P1 · 结论：首版选 DeepSeek API，但只在非敏感白名单上跑通

如果这两天就要定一条路线上线，我建议首版走 DeepSeek 云端 API，同时把“哪些文档可以入库”做成硬白名单，客户合同、个人信息类文档这一版完全不接。理由不是 API 更聪明，而是两周、一名后端工程师这个约束下，云端的工程量几乎只剩检索和权限，模型服务不用自己搭。本地部署 Qwen 并不是错，但它把模型下载、推理引擎安装、显存调参、服务和端口暴露这几件事都压到了同一个人身上 [E12][E13][E14]，这些工作在第一版里不产生用户价值。取舍很清楚：API 路线用“敏感文档先不接”换上线速度，等试用里检索和权限跑稳，再评估本地化。需要说明的是，本文没有实测两条路线的吞吐、延迟或显存占用，任何规模判断都要靠你们自己的小规模验收。

## P2 · 两条首版路线的关键路径差异

云端路线的关键路径是：文档摄取与分块、向量与关键词检索、把召回片段发给 DeepSeek 生成答案、再做引用展示。开源样本里有现成的三段式实现：抽取 PDF 文本、用 HuggingFace Embeddings 向量化写入 ChromaDB、查询时召回相关片段、再送给外部 LLM 生成答案 [E8][E9]。注意这条链路里，召回出来的原文会离开内网 [E9]，所以白名单必须在入库和检索之前拦住，不能靠事后检查。本地路线的关键路径多出三步：用 vLLM 把 Qwen 服务化成 HTTP 接口（示例是通过 tensor-parallel-size 设并行度）[E11]，从模型仓库下载权重 [E13]，以及处理推理服务的网络暴露和访问控制 [E14]。这两条路线的检索层、权限层和引用逻辑其实可以共用，差别主要在生成侧是调外部接口还是调内网服务，所以第一版按 API 设计、把生成调用抽象成一个可替换的客户端，后面换本地模型不会推翻前面的工作。

## P3 · 检索：混合召回，元数据是引用的地基

中文为主的产品手册和研发规范，单靠向量检索容易漏掉型号、命令、术语这类精确匹配。样本项目的做法是递归字符分块加重叠 [E2]，但它把 BM25 加向量嵌入的混合召回列为待改进项，说明这不是现成能力，要自己补 [E10]。首版我建议直接在召回层做向量加关键词的混合，关键词侧用适合中文的分词。引用能不能定位，取决于入库时有没有把元数据写进每个分块：来源文件名、页码、章节、分块 ID [E4]。这件事必须在摄取阶段做，不能等答案生成后回填。一个现实风险是，按字符切分本身不保证段落和 PDF 页码边界完整 [E2]，所以对 PDF 要按页抽取再切，跨页片段要记录起止页。开源参考项目用了 pypdf 和 docx2txt 解析 txt、pdf、docx [E1]，但没有说明是否保留页码和段落定位，这一点不能照搬，要自己加。

## P4 · 资料更新：全量重建先能跑，增量与失效要设计

5000 份文档、10 人使用，首版可以把索引重建做成可触发的后台任务，不必一开始就做复杂增量。样本项目在文档上传或删除时用子进程异步重建整个语义库 [E3]，这个做法简单、行为可预期，适合两周版本；代价是每次变更都是全量重建，5000 份文档重建一次要多久没有材料可依，必须实测 [E3]。同时要防“更新后索引陈旧”这个坑：删除和修改文档必须走同一个入口触发重建，不能让员工直接改共享盘里的文件而索引不知情。如果实测发现全量重建太慢，第二版再引入按文档版本号做差量重建和旧片段失效。首版至少要保证一个语义：文档删除后，旧片段不能再被召回。

## P5 · 权限：检索前按项目过滤，不能只靠提示词

按你们的设定，非敏感文档也不能全员可见，首版至少两个项目隔离。这里的核心原则是权限过滤发生在召回之前，而不是把权限写进提示词让模型自己守规矩。样本项目的进阶架构里把 RBAC、部门权限和文档权限列为结构组件 [E6]，但那是完整阶段的设计，不是两周能落地的全部内容，首版可以简化为“用户—项目”白名单：入库时给每个分块打项目标签，查询时按用户所属项目在向量和关键词检索里都加过滤条件。只过滤向量检索、忘了关键词召回，是典型的越权漏口。验收口径就一条：用 A 项目的账号问 B 项目文档里的具体内容，答案里不能出现该内容，引用里也不能出现该文件。外部 API 路线下还要加一层输入侧拦截，样本材料建议用输入过滤和分类分级防止敏感内容外发 [E18]，落地上就是在文档入库和用户提问两个入口各放一道白名单检查。

## P6 · 本地 Qwen 路线：能做，但两周内不是一个人的活

如果公司后面必须让敏感文档也进问答，本地部署是方向，因为本地推理可以让文档内容全程不出内网 [E16]。但首版就要本地化，需要正视几个工程量：vLLM 要装、模型权重要下载落地 [E12][E13]，推理端点默认只监听本机 8000 端口，接内网试用还要处理网络暴露与访问控制，而权限隔离不在推理服务层完成 [E14]；用容器方式跑时，共享内存等参数配错会报难以定位的运行时错误 [E20]；显存利用率和并发数需要显式调参，单卡并发有拐点，得从小并发往上一路压测找 [E19][E22]。这些经验数据来自视觉模型和特定配置，不能直接换算成你们双 24GB 卡跑文本模型的表现，我不做这个换算。对一名后端工程师来说，这条路线更适合作为试用期之后、有明确敏感文档需求时的二期项目。

## P7 · 要不要混合路线：现在不必

非敏感走云端、敏感走本地的混合架构在技术上成立，样本材料也把它列为兼顾安全与成本的方案 [E17]。但它需要数据分级和自动路由两套额外机制 [E17]，对一个两周上线、单人维护的团队来说，这是把两条路线的复杂度叠加而不是折中。首版的正确做法是用白名单物理隔离：敏感文档根本不进系统，也就不存在错误路由的风险。等试用验证了检索质量和使用意愿，再按“哪些文档必须本地”来划分二期范围。另需提示，云端 API 的数据保留政策，材料只提供了来源自述称默认保留 30 天、企业版可签 DPA [E15]，这是二手描述而非一手条款，如果评审会要对外承诺数据边界，应直接向服务方索取条款原文再定。

## P8 · 最容易踩的五个坑与验收口径

按风险排序：第一，权限过滤只做在向量检索上、漏了关键词侧，导致跨项目召回；第二，PDF 按字符切分破坏页码，答案引用点不到原页，这是分块策略问题不是模型问题 [E2][E4]；第三，更新链路不统一，删除的文档仍被召回 [E3]；第四，外部 API 误发敏感内容，必须靠入库和提问两个入口的前置白名单拦，不能靠人自觉 [E18][E9]；第五，如果后续转本地，显存和并发没有实测就按经验值上线，可能一压测就排队 [E22]。验收就按你们说的 10 人小规模试用，看两件事：随机抽问能不能定位到文件和段落（PDF 到页），以及两个项目之间互相提问不串数据。吞吐和延迟这一版只记录不承诺，等有了真实使用数据再定规模。

## P9 · 两周内的可执行第一步

第 1 周先定两件事：文档白名单和项目标签规则，把两个项目的非敏感文档圈出来，明确“谁属于哪个项目”；同时把摄取管线做出来，保证每个分块带文件名、页码、章节和项目标签 [E4]。第 2 周把检索问答链路串起来，生成侧先接 DeepSeek API，接之前完成入库白名单和提问侧输入检查两道拦截。检索层用向量加关键词混合 [E10]，索引重建先用异步全量重建跑通 [E3]，同时记录单次重建耗时。最后用 10 人试用做验收，按上一段的两个口径判定是否进入下一阶段，并把“是否上本地 Qwen”作为试用结论之后的独立决策，不压进这两周。

## 来源与证据

- [E1] [GitHub - lindejingl-dev/Enterprise-RAG-QA: An enterprise-grade RAG QA system supporting TXT/PDF/DOCX ingestion, vector index auto-rebuilding, and memory-enabled chat using DeepSeek and FAISS. · GitHub](https://github.com/lindejingl-dev/Enterprise-RAG-QA)；原文字符 426–615；对象为该开源项目的摄取流程；支持格式限 txt/pdf/docx；是否保留页码、段落定位未说明；文档规模与中文占比未说明。
- [E2] [GitHub - lindejingl-dev/Enterprise-RAG-QA: An enterprise-grade RAG QA system supporting TXT/PDF/DOCX ingestion, vector index auto-rebuilding, and memory-enabled chat using DeepSeek and FAISS. · GitHub](https://github.com/lindejingl-dev/Enterprise-RAG-QA)；原文字符 618–820；分块参数（chunk 大小、重叠长度）未给出；是否针对中文优化未说明；对PDF页码保持的影响未说明。
- [E3] [GitHub - lindejingl-dev/Enterprise-RAG-QA: An enterprise-grade RAG QA system supporting TXT/PDF/DOCX ingestion, vector index auto-rebuilding, and memory-enabled chat using DeepSeek and FAISS. · GitHub](https://github.com/lindejingl-dev/Enterprise-RAG-QA)；原文字符 965–1156；更新方式为通过 FastAPI 接口程序化上传/删除触发；重建为整库重建而非明确增量；单次重建耗时与并发影响未说明。
- [E4] [How to Build Your Own RAG Knowledge Base Q&A System with DeepSeek - DEV Community](https://dev.to/cheng_zhang_45ee857b979b0/how-to-build-your-own-rag-knowledge-base-qa-system-with-deepseek-3dl9)；原文字符 10425–10571；原文为通用RAG实践建议；未限定文档规模、语言或部署方式；未说明页码在跨页分块下的精确性
- [E6] [How to Build Your Own RAG Knowledge Base Q&A System with DeepSeek - DEV Community](https://dev.to/cheng_zhang_45ee857b979b0/how-to-build-your-own-rag-knowledge-base-qa-system-with-deepseek-3dl9)；原文字符 4266–4335；该行出现在原文的进阶架构建议表中，非首版最小方案；原文未给出过滤实现细节、是否检索前过滤及测试验证方式
- [E8] [GitHub - AymenGabsi/rag_deepseek_groq_doc_qa: 📄⚡ A RAG (Retrieval-Augmented Generation) Document QA system using Deepseek and Groq for fast and efficient querying over documents. Inspired by this tutorial, this project demonstrates how to set up a high-performance AI-powered question-answering system. · GitHub](https://github.com/AymenGabsi/rag_deepseek_groq_doc_qa)；原文字符 1471–1691；仅针对PDF单文件场景；未说明分块策略、页码元数据保留与增量更新机制；未说明中文文档效果
- [E9] [GitHub - AymenGabsi/rag_deepseek_groq_doc_qa: 📄⚡ A RAG (Retrieval-Augmented Generation) Document QA system using Deepseek and Groq for fast and efficient querying over documents. Inspired by this tutorial, this project demonstrates how to set up a high-performance AI-powered question-answering system. · GitHub](https://github.com/AymenGabsi/rag_deepseek_groq_doc_qa)；原文字符 1620–1768；样本项目把内容发送至Groq云端；是否保留日志、数据留存政策未说明；不涉及权限过滤与项目隔离
- [E10] [GitHub - AymenGabsi/rag_deepseek_groq_doc_qa: 📄⚡ A RAG (Retrieval-Augmented Generation) Document QA system using Deepseek and Groq for fast and efficient querying over documents. Inspired by this tutorial, this project demonstrates how to set up a high-performance AI-powered question-answering system. · GitHub](https://github.com/AymenGabsi/rag_deepseek_groq_doc_qa)；原文字符 2066–2137；列为“计划改进”而非已实现功能；未说明中文分词与BM25适配；未给出效果数据
- [E11] [使用 vLLM 部署 Qwen2.5 大模型_vllm部署qwen2.5-CSDN博客](https://blog.csdn.net/mnyh35/article/details/149165996)；原文字符 688–759；对象为Qwen2.5-Coder-32B-Instruct-AWQ，推理引擎vLLM，示例配置4路张量并行；是否适配双24GB卡、显存占用与吞吐均未说明，需实测；未涉及检索、权限或文档摄取。
- [E12] [使用 vLLM 部署 Qwen2.5 大模型_vllm部署qwen2.5-CSDN博客](https://blog.csdn.net/mnyh35/article/details/149165996)；原文字符 536–552；操作对象为Linux终端或SSH连接服务器（文中提及若用Windows需另装C++ Build Tools）；未说明CUDA与驱动版本要求，也未说明安装失败时的排障成本。
- [E13] [使用 vLLM 部署 Qwen2.5 大模型_vllm部署qwen2.5-CSDN博客](https://blog.csdn.net/mnyh35/article/details/149165996)；原文字符 614–674；示例通过HF_ENDPOINT=https://hf-mirror.com镜像下载Qwen2.5-Coder-32B-Instruct-AWQ；权重体积、下载耗时与存储需求未说明。
- [E14] [使用 vLLM 部署 Qwen2.5 大模型_vllm部署qwen2.5-CSDN博客](https://blog.csdn.net/mnyh35/article/details/149165996)；原文字符 824–868；示例以Postman在本机调用；未说明多用户并发、鉴权、按项目过滤或HTTPS配置；是否改绑到内网地址未说明。
- [E15] [DeepSeek 数据隐私与安全性深度分析：企业用户须知 | DeepSeek AI Blog](https://freedeepseekapi.com/zh/blog/deepseek-data-privacy-security-analysis)；原文字符 441–538；对象为DeepSeek云端API；具体计费版本、企业版开通条件与DPA文本未说明；本地部署部分另述不接触数据。
- [E16] [DeepSeek 数据隐私与安全性深度分析：企业用户须知 | DeepSeek AI Blog](https://freedeepseekapi.com/zh/blog/deepseek-data-privacy-security-analysis)；原文字符 982–1038；指DeepSeek开源模型的本地部署；文中未涉及Qwen；未说明具体硬件是否够用，也未给出吞吐或显存实测。
- [E17] [DeepSeek 数据隐私与安全性深度分析：企业用户须知 | DeepSeek AI Blog](https://freedeepseekapi.com/zh/blog/deepseek-data-privacy-security-analysis)；原文字符 2123–2234；适用于中大型企业场景的泛化描述；未给出实现细节、成本或验证数据；本文所述VPN为IPSec/WireGuard加密隧道。
- [E18] [DeepSeek 数据隐私与安全性深度分析：企业用户须知 | DeepSeek AI Blog](https://freedeepseekapi.com/zh/blog/deepseek-data-privacy-security-analysis)；原文字符 6732–6838；针对员工使用AI的一般性建议，非针对文档问答助手的检索链路；未说明与项目级权限过滤的配合方式，也未给出落地成本。
- [E19] [Qwen2-VL与vLLM部署实战：单卡多卡启动与批量推理全指南-CSDN博客](https://blog.csdn.net/weixin_29047545/article/details/168041183)；原文字符 3848–3955；单卡24GB、Qwen2-VL-7B-Instruct、FP16、vLLM 0.8.x；并发与上下文长度在文中另设为batch size 1、32768上下文，非50人公司内网场景
- [E20] [Qwen2-VL与vLLM部署实战：单卡多卡启动与批量推理全指南-CSDN博客](https://blog.csdn.net/weixin_29047545/article/details/168041183)；原文字符 2237–2328；vLLM官方Docker镜像、多模态输入场景；纯文本模型是否同样需要该配置文中未说明
- [E22] [Qwen2-VL与vLLM部署实战：单卡多卡启动与批量推理全指南-CSDN博客](https://blog.csdn.net/weixin_29047545/article/details/168041183)；原文字符 11497–11649；Qwen2-VL-7B单卡24GB、多模态请求；并发拐点与延迟数值仅对该配置与输入有效
