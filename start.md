
aws-inventory-metadata-discovery

这个项目的需求开发一组服务实现aws 账号下的资源数据扫描已经提供给消费者使用
背景：
我们有一个center账号，账号在ap-east-1/eu-west-1/eu-west-2/us-east-1 种都配置了私有VPC 也配置了aws 服务的regional endpoint，在ap-east-1 的VPC中使用了RDS 服务，部署了DB
DB 中有一张account list 表

需求

基本设计
aws-inventory-account-directory lambda 部署在vpc中 他提供从RDS account table读取账号的能力
aws-adfs-saml-broker lamdba 部署在vpc中，他可以连接我们公司的内网获取请求 账号的Readonly的认证信息，如果没有权限那么返回错误
inventory discover 设计
我们需要设计两部分
part 1 ： 通过定时任务调用account-directory 服务获取需要扫描的account，然后获取account的enabled regions，然后下发任务去扫描，包含不同账号 不同region， 不同资源（ecs/ec2/ami/vpc 等，扫描最小力度支持 单个ARN（accoun/region/type/id）,周期4个小时一次，数据存储起来 供消费者使用
part 2: 一个api 服务 他提供获取扫描数据的能力 包含fiter 分页 数量统计，还能够触发扫描任务，支持 扫描账号的所有资源，扫描账号某个region的所有资源，扫描账号某个region的type所有资源，扫描某个arn代表的资源
消费者主要使用这个api 获取 账号的真实resource信息，如果消费者提供了修改某个资源的信息，可以通过调用这个api服务获取修改后的资源数据，当然要考虑 更新保存的数据 来满足 存储数据为真实情况的映射

那么给予以上需求，提供设计方案，可以使用aws的所有原生服务，api使用fastapi开发 部署在ecs上，先不考虑权限问题，没有权限的账号会在adfs阶段就阻拦了。

Python：封装和复用,资源的扫描 也实现按照source type的隔离，要能进行其他资源的拓展，所以设计时要考虑这一点，oop等
然后账号在1000+。region 10左右 ，每个region 资源 比如ecs 最多会达到 1500+

网络上 其他三个region的vpc 和ap-east-1 通过vpc peering，可以连接到ap-east-1的rds,先考虑这四个regions


broker 的细节：自己实现的，原理是通过用户密码 认证内部adfs saml 服务 拿到saml reponse 然后使用sts的assume with saml 获取access token等，所以可以返回saml 也可以返回acccess token
"消费者提供了修改资源的信息"具体是什么意思？
A. 消费者调用api 传入arn，我们去 AWS 重新拉一次（我推荐这个，真实数据永远来自 AWS）

要不要变更历史？可以使用用 S3 快照配合 Athena。但是要提供api
返回数据的形态：有些统一值 accout/region/arn/id/tags(jsonb)/last_update_at 等 然后其他的data jsonb 储存原始值


先讨论方案 不急的落地
