export const SCHEMA_VERSION='UTT-READMODEL-1/1.0.0';
export const PARSER_VERSION='utt-parser/1.0.0';
export const RULE_VERSION='utt-relevance/1.0.0';
export const INDEX_VERSION='utt-index/1.1.0';
export const BUILD_ID='A-PERF-20261028.1';
export function cacheKey(kind,id,fingerprint){return [kind,id,fingerprint,SCHEMA_VERSION,PARSER_VERSION,RULE_VERSION,INDEX_VERSION].join('|');}
